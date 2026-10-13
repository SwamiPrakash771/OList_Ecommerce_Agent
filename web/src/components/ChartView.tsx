// A chart drawn by the admin assistant: line, area, bar (columns or horizontal, grouped or stacked), pie /
// donut, or scatter. Every value comes from the server's metric layer (show_chart), never from the model,
// and so do the colours (palette order or the admin's choice) and the dash patterns. Loaded lazily, so
// pages without charts don't pay for the chart library.

import { useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, LabelList, Line, LineChart, Pie, PieChart, ResponsiveContainer, Scatter,
  ScatterChart, Tooltip, XAxis, YAxis, ZAxis,
} from 'recharts'
import type { ChartSpec } from '../chat'

// Fallback for charts stored before colours were sent: the reference palette's fixed order.
const SLOTS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948']
const INK = '#64748b'
const GRID = '#e2e8f0'
const AXIS = { fontSize: 11, fill: INK }

function formatter(unit: string | null | undefined, compact: boolean) {
  return (v: unknown) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return 'n/a'
    const n = Number(v)
    if (unit === '$') {
      if (compact && Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`
      if (compact && Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(0)}k`
      return `$${n.toLocaleString(undefined, { minimumFractionDigits: compact ? 0 : 2, maximumFractionDigits: 2 })}`
    }
    if (unit === '%') return `${n.toFixed(compact ? 0 : 1)}%`
    const text = compact && Math.abs(n) >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : n.toLocaleString(undefined, { maximumFractionDigits: 2 })
    return unit && !compact ? `${text} ${unit}` : text
  }
}

const metricName = (definition: string | null | undefined) => (definition ?? '').split(':')[0]

type Point = ChartSpec['data'][number]
type TipProps = { active?: boolean; label?: string; payload?: { dataKey: string; name?: string; value: number; color: string; payload: Point }[] }

/** Recharts fills in active / label / payload when it renders this element. */
function ChartTip({ active, label, payload, chart }: TipProps & { chart: ChartSpec }) {
  if (!active || !payload?.length) return null
  const full = formatter(chart.unit, false)
  const point = payload[0].payload
  const multi = chart.series.length > 1
  if (chart.kind === 'scatter') {
    return (
      <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs shadow-md">
        <div className="font-semibold text-slate-900">{point.x}</div>
        <div className="text-slate-700">{metricName(chart.definition)}: <b>{full(point.s0)}</b></div>
        <div className="text-slate-700">{metricName(chart.y_definition)}: <b>{formatter(chart.y_unit, false)(point.y)}</b></div>
      </div>
    )
  }
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs shadow-md">
      <div className="font-semibold text-slate-900">{String(label ?? point.x).replace(/\*$/, '')}</div>
      {payload.map((p) => (
        <div key={p.dataKey + String(p.name)} className="mt-0.5 flex items-center gap-1.5 text-slate-700">
          {multi && <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />}
          {multi && <span>{chart.series.find((s) => s.key === p.dataKey)?.name}:</span>}
          {chart.kind === 'pie' && <span>{point.x}:</span>}
          <span className="font-medium tabular-nums">{full(p.value)}</span>
          {chart.kind === 'pie' && point.share != null && <span className="text-slate-500">({point.share.toFixed(1)}%)</span>}
        </div>
      ))}
      {point.compared_with && <div className="mt-1 text-slate-500">vs {point.compared_with}</div>}
      {point.flag && <div className="mt-1 text-amber-700">{point.flag}</div>}
    </div>
  )
}

/** A point coloured by the chart's above/below rule (lines and areas). */
function ruleDot({ cx, cy, index }: { cx?: number; cy?: number; index?: number }, fill: string) {
  if (cx == null || cy == null) return <g key={index} />
  return <circle key={index} cx={cx} cy={cy} r={4} fill={fill} stroke="#fff" strokeWidth={1.5} />
}

function Swatch({ colour, dash, line }: { colour: string; dash?: string | null; line: boolean }) {
  return line ? (
    <svg width="18" height="8" aria-hidden="true"><line x1="0" y1="4" x2="18" y2="4" stroke={colour} strokeWidth="2" strokeDasharray={dash ?? undefined} /></svg>
  ) : (
    <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: colour }} />
  )
}

export default function ChartView({ chart }: { chart: ChartSpec }) {
  const [asTable, setAsTable] = useState(false)
  const full = formatter(chart.unit, false)
  const axis = formatter(chart.unit, true)
  const colour = (i: number) => chart.series[i]?.color ?? SLOTS[i % SLOTS.length]
  const isLine = chart.kind === 'line' || chart.kind === 'area'
  // Category axes (states, categories, sellers) show every label; angled when there are many.
  const categorical = !/^\d{4}(-|$)/.test(chart.data[0]?.x ?? '')
  const crowded = categorical && chart.data.length > 8
  const stack = chart.stacked ? 'total' : undefined
  // Flagged points (partial period, few orders, hidden) are marked on the axis with an asterisk.
  const data: (Point & { label: string })[] = chart.data.map((d) => ({ ...d, label: d.flag ? `${d.x}*` : d.x }))
  // A point's own colour under the above/below rule (c0, c1... per series), else its series colour.
  const pointColour = (d: Point | undefined, i: number) => ((d?.[`c${i}`] as string | undefined) ?? colour(i))
  const tip = <Tooltip content={<ChartTip chart={chart} />} cursor={isLine ? { stroke: '#94a3b8', strokeDasharray: '4 4' } : { fill: '#f1f5f9' }} />

  const height = chart.kind === 'pie' ? 280 : chart.horizontal ? Math.max(160, data.length * 26 + 40) : crowded ? 320 : 260
  const rule = chart.color_rule
  // Value groups (threshold, quantiles, ranges) replace the series swatches; older charts sent above/below only.
  const legend = rule
    ? rule.legend?.map((g) => ({ name: g.label, colour: g.color, dash: null })) ??
      [{ name: rule.above_label ?? '', colour: rule.above ?? INK, dash: null }, { name: rule.below_label ?? '', colour: rule.below ?? INK, dash: null }]
    : chart.kind === 'pie'
      ? data.map((d, i) => ({ name: d.x + (d.share != null ? ` · ${d.share.toFixed(1)}%` : ''), colour: d.color ?? SLOTS[i % SLOTS.length], dash: null }))
      : chart.series.length > 1
        ? chart.series.map((s, i) => ({ name: s.name, colour: colour(i), dash: s.dash }))
        : []

  const plot = () => {
    if (chart.kind === 'pie') {
      return (
        <PieChart>
          {tip}
          <Pie data={data} dataKey="s0" nameKey="x" innerRadius={chart.donut ? '55%' : 0} outerRadius="90%" paddingAngle={data.length > 1 ? 1 : 0}
            stroke="#fff" strokeWidth={2} isAnimationActive={false} labelLine={false}
            label={data.length <= 12 ? ({ percent }: { percent?: number }) => ((percent ?? 0) >= 0.05 ? `${((percent ?? 0) * 100).toFixed(0)}%` : '') : false}>
            {data.map((d, i) => <Cell key={d.x} fill={d.color ?? SLOTS[i % SLOTS.length]} />)}
          </Pie>
        </PieChart>
      )
    }
    if (chart.kind === 'scatter') {
      return (
        <ScatterChart margin={{ left: 4, right: 12, top: 8 }}>
          <CartesianGrid stroke={GRID} />
          <XAxis type="number" dataKey="s0" name={metricName(chart.definition)} tickFormatter={axis} tick={AXIS} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
          <YAxis type="number" dataKey="y" name={metricName(chart.y_definition)} tickFormatter={formatter(chart.y_unit, true)} tick={AXIS} tickLine={false} axisLine={false} width={52} />
          <ZAxis range={[60, 60]} />
          {tip}
          <Scatter data={data} fill={colour(0)} fillOpacity={0.8} stroke="#fff" strokeWidth={1} isAnimationActive={false}>
            {rule && data.map((d, j) => <Cell key={j} fill={pointColour(d, 0)} />)}
          </Scatter>
        </ScatterChart>
      )
    }
    if (chart.kind === 'bar' && chart.horizontal) {
      return (
        <BarChart data={data} layout="vertical" margin={{ left: 4, right: 12 }}>
          <CartesianGrid stroke={GRID} horizontal={false} />
          <XAxis type="number" tickFormatter={axis} tick={AXIS} tickLine={false} axisLine={false} />
          <YAxis type="category" dataKey="label" width={140} interval={0} tick={{ fontSize: 11, fill: '#334155' }} tickLine={false} axisLine={false} />
          {tip}
          {chart.series.map((s, i) => (
            <Bar key={s.key} dataKey={s.key} stackId={stack} fill={colour(i)} radius={stack ? 0 : [0, 4, 4, 0]} isAnimationActive={false}>
              {rule && data.map((d, j) => <Cell key={j} fill={pointColour(d, i)} />)}
              {chart.show_values && <LabelList dataKey={s.key} position="right" formatter={axis} style={{ fontSize: 10, fill: '#334155' }} />}
            </Bar>
          ))}
        </BarChart>
      )
    }
    if (chart.kind === 'bar') {
      return (
        <BarChart data={data} margin={{ left: 4, right: 8, bottom: crowded ? 40 : 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: '#cbd5e1' }}
            interval={categorical ? 0 : 'preserveStartEnd'} angle={crowded ? -40 : 0} textAnchor={crowded ? 'end' : 'middle'}
            height={crowded ? 70 : 30} />
          <YAxis tickFormatter={axis} tick={AXIS} tickLine={false} axisLine={false} width={52} />
          {tip}
          {chart.series.map((s, i) => (
            <Bar key={s.key} dataKey={s.key} stackId={stack} fill={colour(i)} radius={stack ? 0 : [4, 4, 0, 0]} isAnimationActive={false}>
              {rule && data.map((d, j) => <Cell key={j} fill={pointColour(d, i)} />)}
              {chart.show_values && <LabelList dataKey={s.key} position="top" formatter={axis} style={{ fontSize: 10, fill: '#334155' }} />}
            </Bar>
          ))}
        </BarChart>
      )
    }
    if (chart.kind === 'area') {
      return (
        <AreaChart data={data} margin={{ left: 4, right: 12, top: 4 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
          <YAxis tickFormatter={axis} tick={AXIS} tickLine={false} axisLine={false} width={52} />
          {tip}
          {chart.series.map((s, i) => (
            <Area key={s.key} dataKey={s.key} stackId={stack} stroke={colour(i)} fill={colour(i)} fillOpacity={stack ? 0.85 : 0.15}
              strokeWidth={2} strokeDasharray={s.dash ?? undefined} isAnimationActive={false}
              dot={rule ? (p: { cx?: number; cy?: number; index?: number }) => ruleDot(p, pointColour(data[p.index ?? 0], i)) : false} />
          ))}
        </AreaChart>
      )
    }
    return (
      <LineChart data={data} margin={{ left: 4, right: 12, top: 4 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
        <YAxis tickFormatter={axis} tick={AXIS} tickLine={false} axisLine={false} width={52} />
        {tip}
        {chart.series.map((s, i) => (
          <Line key={s.key} dataKey={s.key} stroke={rule ? '#94a3b8' : colour(i)} strokeWidth={2} strokeDasharray={s.dash ?? undefined}
            dot={rule ? (p: { cx?: number; cy?: number; index?: number }) => ruleDot(p, pointColour(data[p.index ?? 0], i)) : false}
            connectNulls={false} activeDot={{ r: 5, stroke: '#fff', strokeWidth: 2 }} isAnimationActive={false} />
        ))}
      </LineChart>
    )
  }

  const columns = chart.kind === 'scatter'
    ? [{ key: 's0', name: metricName(chart.definition), fmt: full }, { key: 'y', name: metricName(chart.y_definition), fmt: formatter(chart.y_unit, false) }]
    : chart.series.map((s) => ({ key: s.key, name: s.name, fmt: full }))

  return (
    <figure className="my-2 rounded-xl border border-slate-200 bg-white p-3">
      <figcaption className="flex items-start justify-between gap-2">
        <span className="text-sm font-semibold text-slate-900">{chart.title}</span>
        <button onClick={() => setAsTable(!asTable)} className="shrink-0 rounded px-2 py-0.5 text-[11px] text-brand-600 hover:bg-brand-50">
          {asTable ? 'Show chart' : 'Show table'}
        </button>
      </figcaption>
      {legend.length > 0 && !asTable && (
        <div className="mt-1 flex max-h-28 flex-wrap gap-x-3 gap-y-1 overflow-y-auto text-xs text-slate-600" aria-label="Legend">
          {legend.map((l) => (
            <span key={l.name} className="flex items-center gap-1.5"><Swatch colour={l.colour} dash={l.dash} line={isLine} />{l.name}</span>
          ))}
        </div>
      )}

      {asTable ? (
        <div className="mt-2 max-h-72 overflow-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-slate-500">
              <tr>
                <th className="py-1 pr-3 font-semibold">{chart.x_label}</th>
                {columns.map((c) => <th key={c.key} className="py-1 pr-3 text-right font-semibold">{c.name}</th>)}
                {chart.kind === 'pie' && <th className="py-1 pr-3 text-right font-semibold">Share</th>}
              </tr>
            </thead>
            <tbody>
              {chart.data.map((d) => (
                <tr key={d.x} className="border-t border-slate-100">
                  <td className="py-1 pr-3">{d.x}{d.flag && <span className="text-amber-700"> ({d.flag})</span>}</td>
                  {columns.map((c) => <td key={c.key} className="py-1 pr-3 text-right tabular-nums">{c.fmt(d[c.key])}</td>)}
                  {chart.kind === 'pie' && <td className="py-1 pr-3 text-right tabular-nums">{d.share != null ? `${d.share.toFixed(1)}%` : 'n/a'}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="mt-2" style={{ height }}>
          <ResponsiveContainer width="100%" height="100%">{plot()}</ResponsiveContainer>
        </div>
      )}

      <p className="mt-2 text-[11px] leading-snug text-slate-500">
        {chart.plotted && chart.plotted.split(':')[0] !== chart.title.split(' by ')[0] && <>{chart.plotted} · </>}
        {chart.definition}
        {chart.filters && ` · only ${chart.filters}`}
        {chart.y_definition && ` · vs ${chart.y_definition}`} · {chart.period.start} to {chart.period.end}
        {chart.figures_as_of && ` · figures as of ${chart.figures_as_of}`}
        {chart.data.some((d) => d.flag) && ' · * partial period, few orders or hidden for privacy'}
      </p>
      {chart.notes.map((n) => <p key={n} className="mt-0.5 text-[11px] text-amber-700">{n}</p>)}
    </figure>
  )
}
