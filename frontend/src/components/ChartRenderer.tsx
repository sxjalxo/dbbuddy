/**
 * Shared chart renderer used by both the analyst Infographics preview and the
 * published client report, so a chart looks identical wherever it appears.
 *
 * Supports 8 chart types over a flat tabular result (first column = category /
 * x-axis, remaining numeric columns = series) plus a plain table. Colors come
 * from a `ChartConfig`: a named base palette, with optional per-series and
 * per-category overrides that win over the palette.
 */
import { useMemo } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

export type ChartType =
  | "bar"
  | "column"
  | "line"
  | "area"
  | "pie"
  | "doughnut"
  | "scatter"
  | "combo"
  | "table";

/**
 * The stored visual config. This module is the *only* thing that interprets it —
 * the API, publishing, and the CLI pass it around as an opaque blob — so
 * swapping the charting library only touches this file.
 */
export type ChartConfig = {
  /** Config schema version, stamped by the server. Absent ⇒ v1 (pre-stamp). */
  version?: number;
  palette?: string;
  /** Override color per numeric series (column name → hex). */
  seriesColors?: Record<string, string>;
  /** Override color per category / x-axis value (label → hex). */
  categoryColors?: Record<string, string>;
};

/** Config schema version this renderer understands. Bump + migrate on redesign. */
export const CHART_CONFIG_VERSION = 1;

export const CHART_TYPES: { value: ChartType; label: string }[] = [
  { value: "bar", label: "Bar (horizontal)" },
  { value: "column", label: "Column (vertical)" },
  { value: "line", label: "Line" },
  { value: "area", label: "Area" },
  { value: "pie", label: "Pie" },
  { value: "doughnut", label: "Doughnut" },
  { value: "scatter", label: "Scatter" },
  { value: "combo", label: "Combo (bar + line)" },
  { value: "table", label: "Table" },
];

export const PALETTES: Record<string, string[]> = {
  default: ["#6366f1", "#22c55e", "#f59e0b", "#ec4899", "#06b6d4", "#8b5cf6", "#ef4444", "#14b8a6"],
  ocean: ["#0ea5e9", "#0891b2", "#2563eb", "#14b8a6", "#6366f1", "#3b82f6", "#06b6d4", "#0284c7"],
  sunset: ["#f97316", "#ef4444", "#ec4899", "#f59e0b", "#eab308", "#fb7185", "#f43f5e", "#fbbf24"],
  forest: ["#16a34a", "#65a30d", "#22c55e", "#84cc16", "#10b981", "#4d7c0f", "#15803d", "#a3e635"],
  grape: ["#8b5cf6", "#a855f7", "#6366f1", "#d946ef", "#7c3aed", "#c084fc", "#9333ea", "#e879f9"],
  slate: ["#64748b", "#475569", "#94a3b8", "#334155", "#0f172a", "#cbd5e1", "#1e293b", "#7c8698"],
};

export type Row = Record<string, unknown>;

/** Accept either matrix rows (`[[v,v],…]`) or object rows and normalize to objects. */
export function toObjectRows(columns: string[], rows: unknown[]): Row[] {
  if (rows.length === 0) return [];
  if (Array.isArray(rows[0])) {
    return (rows as unknown[][]).map((r) => {
      const o: Row = {};
      columns.forEach((c, i) => (o[c] = r[i]));
      return o;
    });
  }
  return rows as Row[];
}

function isNumericColumn(rows: Row[], col: string): boolean {
  return rows.some(
    (r) =>
      typeof r[col] === "number" || (r[col] !== "" && r[col] != null && !isNaN(Number(r[col]))),
  );
}

/**
 * What can be individually recolored for a given chart, so the customizer knows
 * which pickers to show:
 *  - "series"  → one color per numeric column (bar/column/line/area/combo w/ >1 series)
 *  - "category" → one color per x-axis value (pie/doughnut, or a single-series bar/column)
 *  - "none" → not element-colorable (scatter, table)
 */
export function colorTargets(
  type: ChartType,
  columns: string[],
  rows: unknown[],
): { mode: "series" | "category" | "none"; keys: string[] } {
  const data = toObjectRows(columns, rows);
  const xKey = columns[0];
  const numericKeys = columns.slice(1).filter((c) => isNumericColumn(data, c));

  if (type === "pie" || type === "doughnut") {
    return { mode: "category", keys: data.map((r) => String(r[xKey])) };
  }
  if (type === "scatter" || type === "table") {
    return { mode: "none", keys: [] };
  }
  // bar / column / line / area / combo
  if (numericKeys.length <= 1) {
    // A single measure → color each category (bar/point) individually.
    return { mode: "category", keys: data.map((r) => String(r[xKey])) };
  }
  return { mode: "series", keys: numericKeys };
}

export function ChartRenderer({
  columns,
  rows,
  type = "column",
  config,
  height = 320,
}: {
  columns: string[];
  rows: unknown[];
  type?: ChartType;
  config?: ChartConfig | null;
  height?: number;
}) {
  const data = useMemo(() => toObjectRows(columns, rows), [columns, rows]);
  const xKey = columns[0];
  const numericKeys = useMemo(
    () => columns.slice(1).filter((c) => isNumericColumn(data, c)),
    [columns, data],
  );

  const palette = PALETTES[config?.palette ?? "default"] ?? PALETTES.default;
  const seriesColor = (col: string, i: number) =>
    config?.seriesColors?.[col] ?? palette[i % palette.length];
  const categoryColor = (label: string, i: number) =>
    config?.categoryColors?.[label] ?? palette[i % palette.length];

  // Numeric-coerced copy for charting.
  const chartData = useMemo(
    () =>
      data.map((r) => {
        const o: Row = { [xKey]: r[xKey] };
        numericKeys.forEach((k) => (o[k] = Number(r[k])));
        return o;
      }),
    [data, xKey, numericKeys],
  );

  const empty = data.length === 0;
  const singleSeries = numericKeys.length <= 1;
  const canChart = !!xKey && numericKeys.length > 0;

  // ---- Table (explicit, or fallback when a chart can't be drawn) ----
  if (type === "table" || (!canChart && type !== "scatter") || empty) {
    return <DataTable columns={columns} rows={data} />;
  }

  const grid = <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />;
  const xAxis = <XAxis dataKey={xKey} tick={{ fontSize: 11 }} />;
  const yAxis = <YAxis tick={{ fontSize: 11 }} />;

  const wrap = (child: React.ReactElement) => (
    <div style={{ height, width: "100%" }}>
      <ResponsiveContainer width="100%" height="100%">
        {child}
      </ResponsiveContainer>
    </div>
  );

  // ---- Pie / Doughnut ----
  if (type === "pie" || type === "doughnut") {
    const valueKey = numericKeys[0];
    if (!valueKey) return <DataTable columns={columns} rows={data} />;
    return wrap(
      <PieChart>
        <Tooltip />
        <Legend />
        <Pie
          data={chartData}
          dataKey={valueKey}
          nameKey={xKey}
          cx="50%"
          cy="50%"
          outerRadius="80%"
          innerRadius={type === "doughnut" ? "55%" : 0}
          label
        >
          {chartData.map((r, i) => (
            <Cell key={i} fill={categoryColor(String(r[xKey]), i)} />
          ))}
        </Pie>
      </PieChart>,
    );
  }

  // ---- Scatter (x = first numeric, y = second numeric) ----
  if (type === "scatter") {
    const xNum = numericKeys[0];
    const yNum = numericKeys[1];
    if (!xNum || !yNum) return <DataTable columns={columns} rows={data} />;
    return wrap(
      <ScatterChart>
        {grid}
        <XAxis dataKey={xNum} type="number" name={xNum} tick={{ fontSize: 11 }} />
        <YAxis dataKey={yNum} type="number" name={yNum} tick={{ fontSize: 11 }} />
        <ZAxis range={[60, 60]} />
        <Tooltip cursor={{ strokeDasharray: "3 3" }} />
        <Scatter data={chartData} fill={seriesColor(yNum, 0)} />
      </ScatterChart>,
    );
  }

  // ---- Line ----
  if (type === "line") {
    return wrap(
      <LineChart data={chartData}>
        {grid}
        {xAxis}
        {yAxis}
        <Tooltip />
        {numericKeys.length > 1 && <Legend />}
        {numericKeys.map((k, i) => (
          <Line key={k} type="monotone" dataKey={k} stroke={seriesColor(k, i)} strokeWidth={2} />
        ))}
      </LineChart>,
    );
  }

  // ---- Area ----
  if (type === "area") {
    return wrap(
      <AreaChart data={chartData}>
        {grid}
        {xAxis}
        {yAxis}
        <Tooltip />
        {numericKeys.length > 1 && <Legend />}
        {numericKeys.map((k, i) => (
          <Area
            key={k}
            type="monotone"
            dataKey={k}
            stroke={seriesColor(k, i)}
            fill={seriesColor(k, i)}
            fillOpacity={0.25}
          />
        ))}
      </AreaChart>,
    );
  }

  // ---- Combo (first series as bars, the rest as lines) ----
  if (type === "combo") {
    return wrap(
      <ComposedChart data={chartData}>
        {grid}
        {xAxis}
        {yAxis}
        <Tooltip />
        <Legend />
        {numericKeys.map((k, i) =>
          i === 0 ? (
            <Bar key={k} dataKey={k} fill={seriesColor(k, i)} radius={[4, 4, 0, 0]} />
          ) : (
            <Line key={k} type="monotone" dataKey={k} stroke={seriesColor(k, i)} strokeWidth={2} />
          ),
        )}
      </ComposedChart>,
    );
  }

  // ---- Bar (horizontal) / Column (vertical) ----
  const horizontal = type === "bar";
  return wrap(
    <BarChart data={chartData} layout={horizontal ? "vertical" : "horizontal"}>
      {grid}
      {horizontal ? (
        <>
          <XAxis type="number" tick={{ fontSize: 11 }} />
          <YAxis type="category" dataKey={xKey} tick={{ fontSize: 11 }} width={90} />
        </>
      ) : (
        <>
          {xAxis}
          {yAxis}
        </>
      )}
      <Tooltip />
      {!singleSeries && <Legend />}
      {numericKeys.map((k, i) => (
        <Bar
          key={k}
          dataKey={k}
          fill={seriesColor(k, i)}
          radius={horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]}
        >
          {singleSeries &&
            chartData.map((r, j) => <Cell key={j} fill={categoryColor(String(r[xKey]), j)} />)}
        </Bar>
      ))}
    </BarChart>,
  );
}

function DataTable({ columns, rows }: { columns: string[]; rows: Row[] }) {
  if (rows.length === 0) {
    return <p className="py-12 text-center text-sm text-muted-foreground">No data.</p>;
  }
  return (
    <div className="max-h-80 overflow-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-muted/60">
          <tr>
            {columns.map((c) => (
              <th key={c} className="px-3 py-2 text-left font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 200).map((row, i) => (
            <tr key={i} className="border-t border-border">
              {columns.map((c) => (
                <td key={c} className="px-3 py-1.5">
                  {String(row[c] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
