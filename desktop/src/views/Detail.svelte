<script lang="ts">
  import { onDestroy } from "svelte";
  import {
    CandlestickSeries,
    ColorType,
    createChart,
    createSeriesMarkers,
    LineStyle,
    type IChartApi,
    type ISeriesApi,
    type ISeriesMarkersPluginApi,
    type IPriceLine,
    type SeriesMarker,
    type Time,
    type UTCTimestamp,
  } from "lightweight-charts";
  import { app } from "../lib/state.svelte";
  import { KIND_LABELS, displayName, fmtPct, fmtPrice, fmtTime, movePct, patternName } from "../lib/format";
  import type { Bar, Levels, Zone } from "../lib/types";

  let tf = $state<"1m" | "15m">("15m");
  let levels = $state<Levels | null>(null);
  let err = $state("");
  let loading = $state(false);
  let el: HTMLDivElement | undefined = $state();
  let chart: IChartApi | null = null;
  let series: ISeriesApi<"Candlestick"> | null = null;
  let markers: ISeriesMarkersPluginApi<Time> | null = null;
  let lines: IPriceLine[] = [];
  let bars: Bar[] = [];

  const sym = $derived(app.detailSymbol);
  const quote = $derived(sym ? app.quotes[sym] : undefined);
  const sigs = $derived(sym ? app.signals.filter((s) => s.symbol === sym) : []);

  function css(name: string): string {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function ensureChart() {
    if (chart || !el) return;
    chart = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: css("--panel") }, textColor: css("--muted") },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      timeScale: { timeVisible: true, secondsVisible: false },
      localization: { locale: "zh-CN" },
    });
    series = chart.addSeries(CandlestickSeries, {
      upColor: css("--up"),
      downColor: css("--down"),
      borderVisible: false,
      wickUpColor: css("--up"),
      wickDownColor: css("--down"),
    });
    markers = createSeriesMarkers(series, []);
  }

  function zoneLines(zs: Zone[], color: string, label: string) {
    if (!series) return;
    for (const z of zs.slice(0, 3)) {
      const style = z.strong ? LineStyle.Solid : LineStyle.Dashed;
      lines.push(series.createPriceLine({ price: z.hi, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title: `${label} ${z.stars}` }));
      if (z.hi !== z.lo) lines.push(series.createPriceLine({ price: z.lo, color, lineWidth: 1, lineStyle: style, axisLabelVisible: false, title: "" }));
    }
  }

  function draw() {
    ensureChart();
    if (!series) return;
    // 15m 的 t 是开始时间；1m 的 t 是开始时间（后端 Bar.ts）
    series.setData(bars.map((b) => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })));
    for (const l of lines) series.removePriceLine(l);
    lines = [];
    if (levels) {
      zoneLines(levels.res, css("--down"), "压力");
      zoneLines(levels.sup, css("--up"), "支撑");
    }
    const step = tf === "15m" ? 900 : 60;
    const first = bars[0]?.t ?? 0;
    const ms: SeriesMarker<Time>[] = sigs
      .filter((s) => (s.kind === "open" || s.kind === "upgrade") && s.ts >= first)
      .map((s) => ({
        time: (Math.floor((s.ts - 1) / step) * step) as UTCTimestamp,
        position: s.direction > 0 ? ("belowBar" as const) : ("aboveBar" as const),
        color: s.direction > 0 ? css("--up") : css("--down"),
        shape: s.direction > 0 ? ("arrowUp" as const) : ("arrowDown" as const),
        text: `${s.level}`,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));
    markers?.setMarkers(ms);
    chart?.timeScale().fitContent();
  }

  async function load() {
    if (!app.client || !sym) return;
    loading = true;
    err = "";
    try {
      const [b, lv] = await Promise.all([app.client.bars(sym, tf, tf === "15m" ? 400 : 600), app.client.levels(sym)]);
      bars = b.items;
      levels = lv;
      draw();
    } catch (e) {
      err = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void sym;
    void tf;
    if (el) void load();
  });

  onDestroy(() => chart?.remove());

  function trendLabel(t?: { label?: string }) {
    return t?.label || "—";
  }
</script>

{#if !sym}
  <p class="muted">从「信号」或「标的」里点一个标的。</p>
{:else}
  <div class="row head">
    <button class="small" onclick={() => (app.view = "symbols")}>← 返回</button>
    <h1>{displayName(sym)}</h1>
    {#if quote}
      <span class="mono price">{fmtPrice(quote.price)}</span>
      <span class="mono" class:up={(quote.day_chg ?? 0) > 0} class:down={(quote.day_chg ?? 0) < 0}>{fmtPct(quote.day_chg, 2, true)}</span>
    {/if}
    <div style="flex: 1"></div>
    <button class="small" class:sel={tf === "1m"} onclick={() => (tf = "1m")}>1 分钟</button>
    <button class="small" class:sel={tf === "15m"} onclick={() => (tf = "15m")}>15 分钟</button>
    <button class="small" onclick={load} disabled={loading}>刷新</button>
  </div>
  {#if err}<p class="err">{err}</p>{/if}

  <div class="chart" bind:this={el}></div>

  <div class="cols">
    <div class="card">
      <h2>趋势和关键位</h2>
      {#if levels?.enabled && levels.trend}
        <p>15 分钟：{trendLabel(levels.trend.s15)} · 1 小时：{trendLabel(levels.trend.s1)} · 4 小时：{trendLabel(levels.trend.s4)}</p>
        <table>
          <tbody>
            {#each levels.res as z}
              <tr><td class="down">压力</td><td class="mono">{fmtPrice(z.lo)}–{fmtPrice(z.hi)}</td><td>{z.stars}</td><td class="muted small">{z.src}{z.strong ? " · 强" : ""}</td></tr>
            {/each}
            {#each levels.sup as z}
              <tr><td class="up">支撑</td><td class="mono">{fmtPrice(z.lo)}–{fmtPrice(z.hi)}</td><td>{z.stars}</td><td class="muted small">{z.src}{z.strong ? " · 强" : ""}</td></tr>
            {/each}
          </tbody>
        </table>
        {#if levels.atr1h}<p class="muted small">ATR(1h) {fmtPrice(levels.atr1h)}。实线为强位。</p>{/if}
      {:else}
        <p class="muted">这个标的没有开趋势监控，或数据还不够。</p>
      {/if}
    </div>
    <div class="card">
      <h2>最近信号</h2>
      {#if sigs.length === 0}
        <p class="muted">最近 36 小时没有信号。</p>
      {:else}
        <table>
          <tbody>
            {#each sigs.slice(0, 30) as s (s.seq)}
              <tr>
                <td class="mono muted">{fmtTime(s.ts)}</td>
                <td class:up={s.direction > 0} class:down={s.direction < 0}>{patternName(s)}</td>
                <td class="muted small">{KIND_LABELS[s.kind]}</td>
                <td><span class="badge {s.level}">{s.level}</span></td>
                <td class="num mono">{fmtPct(movePct(s))}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  </div>
{/if}

<style>
  .head {
    margin-bottom: 10px;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  .price {
    font-size: 18px;
  }
  .chart {
    height: 420px;
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 14px;
  }
  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  @media (max-width: 1000px) {
    .cols {
      grid-template-columns: 1fr;
    }
  }
  button.sel {
    border-color: var(--accent);
    color: var(--accent);
  }
</style>
