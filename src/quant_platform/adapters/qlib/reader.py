"""Optional one-shot component. This is the only module importing Qlib."""

import json
import sys


def read(path, code=None):
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=path, region="cn", kernels=1, expression_cache=None, dataset_cache=None)
    calendar = [str(d.date()) for d in D.calendar(freq="day")]
    members = D.list_instruments(D.instruments("all"), freq="day", as_list=True)
    result = {"calendar": calendar, "instruments": members}
    if code:
        frame = D.features([code], ["$open", "$high", "$low", "$close", "$volume"], freq="day", disk_cache=0)
        frame = frame.reset_index()
        frame["datetime"] = frame.datetime.astype(str)
        result["rows"] = json.loads(frame.to_json(orient="records"))
    return result


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print("QUANT_RESULT=" + json.dumps(read(request["path"], request.get("symbol"))))
