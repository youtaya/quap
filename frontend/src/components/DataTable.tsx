/**
 * 数据表格。三件事：把枚举本地化、把时刻转北京时间、把数字右对齐并等宽。
 *
 * 横向溢出只允许发生在 `.qp-table-wrap` 内部——页面级横向滚动是回归测试的硬指标之一。
 */

import type { ReactNode } from 'react';

import { EmptyState } from './ui';
import { formatDay, formatMoment, formatNumber, text } from '@/domain/format';
import { ACTIONS, BOARDS, FREQUENCIES, KINDS, ORIGINS, PURPOSES, QUEUES, ROLES, STATES } from '@/domain/labels';

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => ReactNode;
  /** 数值列：右对齐 + tabular-nums。 */
  numeric?: boolean;
  width?: string;
}

/**
 * 枚举列的本地化。命中不到就原样显示——不吞信息。
 *
 * 这组映射原先是 `src/quant_platform/dashboard/app.py` 的 `ENUM_COLUMNS` 的镜像；那个文件已随
 * Streamlit 版一起删除，所以这里就是唯一定义，改这里不必再去同步别处。
 */
const ENUM_MAPS: Record<string, Record<string, string>> = {
  board: BOARDS,
  status: STATES,
  state: STATES,
  role: ROLES,
  queue: QUEUES,
  kind: KINDS,
  frequency: FREQUENCIES,
  purpose: PURPOSES,
  origin: ORIGINS,
  action: ACTIONS,
};

/** 时刻列：转北京时间。键与 `app.py` 的 `TIME_COLUMNS` 对应。 */
const TIME_COLUMNS = new Set(['source_time', 'effective_from', 'updated_at', 'available_at', 'valid_until']);
const DAY_COLUMNS = new Set(['day', 'as_of', 'list_date', 'traded_on']);

const BOOLEAN_MAPS: Record<string, [string, string]> = {
  healthy: ['正常', '心跳过期'],
  passed: ['通过', '未通过'],
  valid: ['有效', '无效'],
  fresh: ['新鲜', '过期'],
};

/** 默认单元格渲染：按列名决定怎么显示，规则集中在这里而不是散到每个页面。 */
export function renderCell(key: string, value: unknown): ReactNode {
  if (value === null || value === undefined || value === '') return <span className="qp-muted">—</span>;

  const enumMap = ENUM_MAPS[key];
  if (enumMap) return enumMap[String(value)] ?? String(value);

  const booleanMap = BOOLEAN_MAPS[key];
  if (booleanMap && typeof value === 'boolean') return booleanMap[value ? 0 : 1];

  if (TIME_COLUMNS.has(key)) return <span className="qp-num">{formatMoment(value)}</span>;
  if (DAY_COLUMNS.has(key)) return <span className="qp-num">{formatDay(value)}</span>;

  if (typeof value === 'number') return <span className="qp-num">{formatNumber(value, Number.isInteger(value) ? 0 : 4)}</span>;
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') {
    const rendered = JSON.stringify(value);
    return (
      <span className="qp-caption" title={rendered}>
        {rendered.length > 80 ? `${rendered.slice(0, 80)}…` : rendered}
      </span>
    );
  }
  return text(value);
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[] | null | undefined;
  rowKey: (row: T, index: number) => string;
  empty?: string;
  caption?: string;
}

export function DataTable<T extends Record<string, unknown>>({
  columns,
  rows,
  rowKey,
  empty = '暂无数据',
  caption,
}: DataTableProps<T>) {
  if (!rows || rows.length === 0) return <EmptyState>{empty}</EmptyState>;

  return (
    <div className="qp-table-wrap">
      <table className="qp-table">
        {caption ? <caption className="qp-visually-hidden">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={column.numeric ? 'qp-table__num' : undefined}
                style={column.width ? { width: column.width } : undefined}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)}>
              {columns.map((column) => (
                <td key={column.key} className={column.numeric ? 'qp-table__num' : undefined}>
                  {column.render ? column.render(row) : renderCell(column.key, row[column.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 从一组行里推导列（跳过明显是 JSON 的宽字段），用于「原始证据」这类只读表格。 */
export function inferColumns<T extends Record<string, unknown>>(rows: T[], preferred?: string[]): Column<T>[] {
  if (rows.length === 0) return [];
  const keys = preferred ?? Object.keys(rows[0]!);
  return keys.map((key) => ({
    key,
    header: key,
    numeric: rows.some((row) => typeof row[key] === 'number'),
  }));
}
