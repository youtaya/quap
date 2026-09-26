"""A bounded expression language compiled to trusted Qlib syntax, never Python."""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from quant_platform.domain import digest
from quant_platform.domain.workflow import PipelineBlocked

FIELDS = frozenset({"open", "high", "low", "close", "volume", "vwap", "amount"})
UNARY = frozenset({"Abs", "Log"})
ROLLING = frozenset({"Ref", "Mean", "Std", "Sum", "Min", "Max", "Delta"})
TOKEN = re.compile(r"\s*(\$[a-z_]+|[A-Za-z][A-Za-z0-9_]*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[()+*/,-])")


def research_identity():
    root = Path(__file__).resolve().parents[1]
    paths = (
        "research/factors.py",
        "research/experiments.py",
        "research/diagnostics.py",
        "domain/labels.py",
        "adapters/qlib/data.py",
        "adapters/qlib/runtime.py",
        "adapters/qlib/strategy.py",
    )
    return {
        "sources": {path: sha256((root / path).read_bytes()).hexdigest() for path in paths},
        "dependencies": {"qlib": "0.9.7", "lightgbm": "4.7.0"},
    }


@dataclass(frozen=True)
class Node:
    text: str
    fields: frozenset[str]
    warmup: int


def compile_factor(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 2048:
        raise ValueError("Factor expression must contain 1–2,048 characters.")
    tokens, position = [], 0
    expression = expression.strip()
    while position < len(expression):
        match = TOKEN.match(expression, position)
        if not match:
            raise ValueError("Unsupported character in factor expression.")
        tokens.append(match.group(1))
        position = match.end()
    if len(tokens) > 256:
        raise ValueError("Factor expression exceeds the token limit.")
    index = 0

    def consume(expected=None):
        nonlocal index
        if index >= len(tokens) or (expected is not None and tokens[index] != expected):
            raise ValueError("Malformed factor expression.")
        value = tokens[index]
        index += 1
        return value

    def peek():
        return tokens[index] if index < len(tokens) else None

    def parse(depth=0, minimum=0):
        if depth > 16:
            raise ValueError("Factor expression exceeds the nesting limit.")
        token = consume()
        if token == "(":
            node = parse(depth + 1)
            consume(")")
        elif token in {"+", "-"}:
            child = parse(depth + 1, 3)
            node = Node(f"({token}{child.text})", child.fields, child.warmup)
        elif token.startswith("$"):
            if token[1:] not in FIELDS:
                raise ValueError("Only approved market fields are allowed; labels and future data are forbidden.")
            node = Node(token, frozenset({token[1:]}), 0)
        elif re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", token):
            value = float(token)
            if not 0 <= value <= 1e12:
                raise ValueError("Numeric constants must be finite and bounded.")
            node = Node(format(value, ".15g"), frozenset(), 0)
        elif token in UNARY | ROLLING | {"Corr"}:
            consume("(")
            children = [parse(depth + 1)]
            if token == "Corr":
                consume(",")
                children.append(parse(depth + 1))
            window = None
            if token not in UNARY:
                consume(",")
                raw = consume()
                if not raw.isdigit() or not 1 <= int(raw) <= 252:
                    raise ValueError(
                        "Windows and lags must be integers from 1 to 252; future references are forbidden."
                    )
                window = int(raw)
            consume(")")
            arguments = [child.text for child in children] + ([str(window)] if window is not None else [])
            warmup = max(child.warmup for child in children)
            warmup += (window if token in {"Ref", "Delta"} else window - 1) if window is not None else 0
            node = Node(f"{token}({','.join(arguments)})", frozenset().union(*(c.fields for c in children)), warmup)
        else:
            raise ValueError("Unknown factor operator; arbitrary names and functions are forbidden.")
        precedence = {"+": 1, "-": 1, "*": 2, "/": 2}
        while peek() in precedence and precedence[peek()] >= minimum:
            operator = consume()
            right = parse(depth + 1, precedence[operator] + 1)
            node = Node(
                f"({node.text}{operator}{right.text})", node.fields | right.fields, max(node.warmup, right.warmup)
            )
        return node

    root = parse()
    if index != len(tokens) or not root.fields or root.warmup > 1000:
        raise ValueError(
            "Expression must be complete, reference market data, and require at most 1,000 warm-up sessions."
        )
    return {
        "expression": root.text,
        "expression_hash": digest({"grammar": "quap-factor-v1", "expression": root.text}),
        "required_fields": sorted(root.fields),
        "warmup": root.warmup,
        "grammar": "quap-factor-v1",
        "frequency": "day",
    }


def feature_spec(configuration):
    if configuration is None:
        return None
    identity = {k: v for k, v in configuration.items() if k not in {"id", "revision", "configuration_hash"}}
    if digest(identity) != configuration.get("configuration_hash"):
        raise PipelineBlocked("Training configuration hash mismatch.")
    features = configuration.get("features")
    if not features:
        return None
    if configuration["frequency"] != "day" or len(features) > 64:
        raise PipelineBlocked("Unsupported custom feature contract.")
    expressions, names = [], []
    for feature in features:
        compiled = compile_factor(feature["expression"])
        if compiled["expression_hash"] != feature["expression_hash"] or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,63}", feature["name"]
        ):
            raise PipelineBlocked("Factor revision or expression hash mismatch.")
        expressions.append(compiled["expression"])
        names.append("R_" + feature["name"])
    if len(set(names)) != len(names):
        raise PipelineBlocked("Duplicate feature names.")
    return expressions, names


def development_folds(days, configuration, warmup=60):
    """Three fixed rolling train windows; final test data never enters development."""
    days = sorted(set(days))
    train = configuration["train_sessions"]
    valid = configuration["validation_sessions"]
    test = configuration["test_sessions"]
    purge = 6 if configuration["frequency"] == "day" else 1
    test_start = len(days) - purge - test
    development_end = test_start - purge
    first_valid = development_end - 3 * valid
    first_train = first_valid - purge - train
    if first_train < warmup:
        raise PipelineBlocked(
            "Insufficient history for three fixed development folds, purging, warm-up, and reserved test."
        )
    folds = []
    for number in range(3):
        start = first_valid + number * valid
        folds.append(
            {
                "train": (days[start - purge - train], days[start - purge - 1]),
                "valid": (days[start], days[start + valid - 1]),
            }
        )
    return folds, (days[test_start], days[-purge - 1])
