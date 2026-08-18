/**
 * History timeline — linear 7-day scale, Now on the right, zoom + scroll.
 */
(function () {
  const BASE_PX_PER_MINUTE = 2.2;
  const MIN_ON_WIDTH_PX = 6;
  const MIN_OFF_WIDTH_PX = 1;
  const GAP_MIN_LINEAR_PX = 28;
  const LABEL_WIDTH = 168;
  const MINUTE_MS = 60 * 1000;
  const MIN_TICK_SPACING_PX = 68;
  const TICK_INTERVALS_MIN = [15, 30, 60, 120, 180, 240, 360, 480, 720, 1440, 2880, 4320, 10080];
  const HOURS_DEFAULT = 168;
  const VIEWPORT_HOURS_DEFAULT = 24;
  const STORAGE_EXPANDED = "pumpd-history-timeline-expanded";
  const STORAGE_ZOOM = "pumpd-history-timeline-zoom";
  const STORAGE_VIEWPORT = "pumpd-history-timeline-viewport-hours";

  function parseMs(iso) {
    return new Date(iso).getTime();
  }

  function loadZoom() {
    const raw = parseFloat(sessionStorage.getItem(STORAGE_ZOOM) || "1");
    return Number.isFinite(raw) ? Math.min(12, Math.max(0.2, raw)) : 1;
  }

  function saveZoom(zoom) {
    sessionStorage.setItem(STORAGE_ZOOM, String(zoom));
  }

  function loadViewportHours() {
    const raw = parseFloat(sessionStorage.getItem(STORAGE_VIEWPORT) || String(VIEWPORT_HOURS_DEFAULT));
    return Number.isFinite(raw) && raw > 0 ? raw : VIEWPORT_HOURS_DEFAULT;
  }

  function saveViewportHours(hours) {
    sessionStorage.setItem(STORAGE_VIEWPORT, String(hours));
  }

  function fitZoomForViewportHours(hours, scrollEl) {
    const trackArea = Math.max(280, (scrollEl?.clientWidth || 800) - LABEL_WIDTH - 24);
    const pxPerMinute = trackArea / (hours * 60);
    return Math.max(0.2, Math.min(12, pxPerMinute / BASE_PX_PER_MINUTE));
  }

  function markerSize(zoom) {
    const w = Math.max(4, Math.min(12, 4 + zoom * 1.8));
    const h = Math.max(16, Math.min(30, 16 + zoom * 5));
    return { w, h };
  }

  function loadExpandedState() {
    try {
      return JSON.parse(sessionStorage.getItem(STORAGE_EXPANDED) || "{}");
    } catch {
      return {};
    }
  }

  function saveExpandedState(state) {
    sessionStorage.setItem(STORAGE_EXPANDED, JSON.stringify(state));
  }

  function escapeAttr(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  }

  function formatTs(iso, timeZone) {
    try {
      return new Date(iso).toLocaleString(undefined, {
        timeZone,
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  }

  function trackWidthPx(rangeStartMs, rangeEndMs, pxPerMinute) {
    return Math.max(((rangeEndMs - rangeStartMs) / 60000) * pxPerMinute, 320);
  }

  function msToX(tsMs, rangeStartMs, pxPerMinute) {
    return ((tsMs - rangeStartMs) / 60000) * pxPerMinute;
  }

  function localParts(ms, timeZone) {
    try {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone,
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "numeric",
        minute: "numeric",
        second: "numeric",
        hour12: false,
      }).formatToParts(new Date(ms));
      const get = (type) => Number(parts.find((p) => p.type === type)?.value || 0);
      return {
        year: get("year"),
        month: get("month"),
        day: get("day"),
        hour: get("hour") % 24,
        minute: get("minute"),
        second: get("second"),
      };
    } catch {
      const d = new Date(ms);
      return {
        year: d.getFullYear(),
        month: d.getMonth() + 1,
        day: d.getDate(),
        hour: d.getHours(),
        minute: d.getMinutes(),
        second: d.getSeconds(),
      };
    }
  }

  function msFromLocalParts(parts, timeZone) {
    let guess = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, 0);
    for (let i = 0; i < 4; i += 1) {
      const p = localParts(guess, timeZone);
      const target = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, 0);
      const current = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, 0);
      guess += target - current;
    }
    return guess;
  }

  function isLocalMidnight(ms, timeZone) {
    const p = localParts(ms, timeZone);
    return p.hour === 0 && p.minute === 0;
  }

  function formatTimeOnly(ms, timeZone, withMinutes) {
    try {
      return new Date(ms).toLocaleTimeString(undefined, {
        timeZone,
        hour: "numeric",
        minute: withMinutes ? "2-digit" : undefined,
      });
    } catch {
      return new Date(ms).toLocaleTimeString();
    }
  }

  function formatDateOnly(ms, timeZone, compact) {
    try {
      return new Date(ms).toLocaleDateString(undefined, {
        timeZone,
        weekday: compact ? undefined : "short",
        month: "numeric",
        day: "numeric",
      });
    } catch {
      return new Date(ms).toLocaleDateString();
    }
  }

  function chooseTickIntervalMin(pxPerMinute) {
    const minMinutes = MIN_TICK_SPACING_PX / pxPerMinute;
    return TICK_INTERVALS_MIN.find((interval) => interval >= minMinutes) || 10080;
  }

  function snapLocalTime(ms, intervalMin, timeZone) {
    const p = localParts(ms, timeZone);
    if (intervalMin >= 1440) {
      return msFromLocalParts({ ...p, hour: 0, minute: 0 }, timeZone);
    }
    if (intervalMin >= 60) {
      const stepHours = intervalMin / 60;
      const hour = Math.floor(p.hour / stepHours) * stepHours;
      return msFromLocalParts({ ...p, hour, minute: 0 }, timeZone);
    }
    const step = intervalMin;
    const minute = Math.floor(p.minute / step) * step;
    return msFromLocalParts({ ...p, minute }, timeZone);
  }

  function formatAxisTickLabel(ms, intervalMin, timeZone) {
    const midnight = isLocalMidnight(ms, timeZone);
    if (intervalMin >= 1440) {
      return { primary: formatDateOnly(ms, timeZone, false), secondary: null, major: true };
    }
    if (midnight) {
      return { primary: formatDateOnly(ms, timeZone, intervalMin < 720), secondary: null, major: true };
    }
    if (intervalMin >= 360) {
      return { primary: formatTimeOnly(ms, timeZone, false), secondary: null, major: false };
    }
    if (intervalMin >= 60) {
      return { primary: formatTimeOnly(ms, timeZone, intervalMin < 120), secondary: null, major: false };
    }
    return { primary: formatTimeOnly(ms, timeZone, true), secondary: null, major: false };
  }

  function buildAdaptiveTicks(rangeStartMs, rangeEndMs, pxPerMinute, timeZone) {
    const intervalMin = chooseTickIntervalMin(pxPerMinute);
    const intervalMs = intervalMin * MINUTE_MS;
    const ticks = [];

    let cursor = snapLocalTime(rangeEndMs, intervalMin, timeZone);
    const guard = rangeStartMs - intervalMs * 2;
    while (cursor >= guard) {
      if (cursor >= rangeStartMs - intervalMs * 0.5 && cursor <= rangeEndMs + intervalMs * 0.5) {
        const x = msToX(cursor, rangeStartMs, pxPerMinute);
        const fmt = formatAxisTickLabel(cursor, intervalMin, timeZone);
        ticks.push({
          ms: cursor,
          x,
          label: fmt.primary,
          secondary: fmt.secondary,
          major: fmt.major,
          isNow: false,
        });
      }
      cursor -= intervalMs;
    }

    ticks.push({
      ms: rangeEndMs,
      x: msToX(rangeEndMs, rangeStartMs, pxPerMinute),
      label: "Now",
      secondary: null,
      major: true,
      isNow: true,
    });

    ticks.sort((a, b) => a.x - b.x);
    return decrowdTicks(ticks);
  }

  function decrowdTicks(ticks) {
    const sorted = [...ticks].sort((a, b) => a.x - b.x);
    const nowTick = sorted.find((t) => t.isNow);
    const out = [];
    for (const tick of sorted) {
      if (tick.isNow) continue;
      const last = out[out.length - 1];
      if (!last) {
        out.push(tick);
        continue;
      }
      const gap = tick.x - last.x;
      if (gap >= MIN_TICK_SPACING_PX) {
        out.push(tick);
      } else if (tick.major && !last.major) {
        out[out.length - 1] = tick;
      }
    }
    if (nowTick) {
      while (out.length && nowTick.x - out[out.length - 1].x < MIN_TICK_SPACING_PX * 0.55) {
        out.pop();
      }
      out.push(nowTick);
    }
    return out;
  }

  function renderTimeAxis(rangeStartMs, rangeEndMs, pxPerMinute, timeZone) {
    const width = trackWidthPx(rangeStartMs, rangeEndMs, pxPerMinute);
    const ticks = buildAdaptiveTicks(rangeStartMs, rangeEndMs, pxPerMinute, timeZone);
    const tickHtml = ticks
      .map((tick) => {
        const cls = [
          "tl-axis-tick",
          tick.isNow ? "tl-axis-now" : "",
          tick.major ? "tl-axis-major" : "tl-axis-minor",
        ]
          .filter(Boolean)
          .join(" ");
        const anchor = tick.isNow ? "tl-axis-tick-end" : "";
        const transform = tick.isNow ? "transform:translateX(-100%);" : "transform:translateX(-50%);";
        const secondary = tick.secondary
          ? `<span class="tl-axis-tick-sub">${escapeAttr(tick.secondary)}</span>`
          : "";
        return `<span class="${cls} ${anchor}" style="left:${tick.x}px;${transform}">${escapeAttr(tick.label)}${secondary}</span>`;
      })
      .join("");

    const gridHtml = ticks
      .filter((tick) => !tick.isNow)
      .map((tick) => {
        const gridCls = tick.major ? "tl-axis-grid-major" : "tl-axis-grid-minor";
        return `<span class="tl-axis-grid ${gridCls}" style="left:${tick.x}px"></span>`;
      })
      .join("");

    return `<div class="tl-axis" style="width:${width}px;height:28px;">${gridHtml}${tickHtml}<span class="tl-axis-line"></span></div>`;
  }

  function segmentWidth(startMs, endMs, rangeStartMs, pxPerMinute, kind) {
    const linear = msToX(endMs, rangeStartMs, pxPerMinute) - msToX(startMs, rangeStartMs, pxPerMinute);
    if (kind === "gap") return Math.max(GAP_MIN_LINEAR_PX, Math.min(linear, GAP_MIN_LINEAR_PX * 3));
    if (kind === "on") return Math.max(linear, MIN_ON_WIDTH_PX);
    return Math.max(linear, MIN_OFF_WIDTH_PX);
  }

  function renderLinearTrack(segments, markers, rangeStartMs, rangeEndMs, pxPerMinute, timeZone, zoom) {
    const width = trackWidthPx(rangeStartMs, rangeEndMs, pxPerMinute);
    let segHtml = "";

    for (const seg of segments || []) {
      const startMs = parseMs(seg.start);
      const endMs = parseMs(seg.end);
      if (endMs <= rangeStartMs || startMs >= rangeEndMs) continue;

      const clipStart = Math.max(startMs, rangeStartMs);
      const clipEnd = Math.min(endMs, rangeEndMs);
      const left = msToX(clipStart, rangeStartMs, pxPerMinute);
      const w = segmentWidth(clipStart, clipEnd, rangeStartMs, pxPerMinute, seg.kind);
      const title = `${seg.kind.toUpperCase()} · ${formatTs(seg.start, timeZone)} – ${formatTs(seg.end, timeZone)}`;

      if (seg.kind === "gap") {
        segHtml += `<span class="tl-seg tl-gap" style="left:${left}px;width:${w}px" title="${escapeAttr(title)}">…</span>`;
      } else {
        const cls = seg.kind === "on" ? "tl-on" : "tl-off";
        segHtml += `<span class="tl-seg ${cls}" style="left:${left}px;width:${w}px" title="${escapeAttr(title)}"></span>`;
      }
    }

    const { w: markerW, h: markerH } = markerSize(zoom || 1);
    const markerHtml = (markers || [])
      .map((m) => {
        const ts = parseMs(m.ts);
        if (ts < rangeStartMs || ts > rangeEndMs) return "";
        const x = msToX(ts, rangeStartMs, pxPerMinute);
        let cls = m.event_type === "turn_on" ? "on" : "off";
        if (m.failed) cls += " failed";
        const failNote = m.failed ? " (verify failed)" : "";
        const title = `${m.action}${failNote} · ${formatTs(m.ts, timeZone)}\n${m.pump_name}\n${m.reason || ""}`;
        return `<span class="tl-marker tl-marker-${cls}" data-ts="${escapeAttr(m.ts)}" style="left:${x}px;width:${markerW}px;height:${markerH}px;transform:translateX(-${markerW / 2}px)" title="${escapeAttr(title)}"></span>`;
      })
      .join("");

    return `<div class="tl-track" style="width:${width}px">${segHtml}${markerHtml}</div>`;
  }

  function renderRow(label, trackHtml, toggleHtml, rowClass, trackWidth) {
    return `
      <div class="tl-row ${rowClass || ""}" style="min-width:${LABEL_WIDTH + trackWidth}px">
        <div class="tl-label" style="width:${LABEL_WIDTH}px">${toggleHtml}${escapeAttr(label)}</div>
        <div class="tl-track-wrap" style="width:${trackWidth}px">${trackHtml}</div>
      </div>`;
  }

  function renderRecentEvents(markers, timeZone, timezoneLabel) {
    const recent = [...(markers || [])].sort((a, b) => parseMs(b.ts) - parseMs(a.ts)).slice(0, 24);
    if (!recent.length) return "";
    const items = recent
      .map((m) => {
        const fail = m.failed ? " tl-event-failed" : "";
        const action = m.failed && m.event_type === "turn_on" ? `${m.action}?` : m.action;
        const when = m.ts_local || formatTs(m.ts, timeZone);
        const label = `${when} · ${m.pump_name} · ${action}`;
        return `<li><button type="button" class="tl-event-jump${fail}" data-ts="${escapeAttr(m.ts)}" title="${escapeAttr(m.reason || "")}">${escapeAttr(label)}</button></li>`;
      })
      .join("");
    const tzNote = timezoneLabel ? ` (${escapeAttr(timezoneLabel)})` : "";
    return `<div class="tl-recent-events"><span class="tl-recent-label">Recent on chart${tzNote} — click to jump:</span><ul class="tl-recent-list">${items}</ul></div>`;
  }

  function renderTimeline(data, expanded, zoom, viewportHours) {
    const rangeStartMs = parseMs(data.range_start);
    const rangeEndMs = parseMs(data.range_end);
    const pxPerMinute = BASE_PX_PER_MINUTE * zoom;
    const trackWidth = trackWidthPx(rangeStartMs, rangeEndMs, pxPerMinute);
    const innerWidth = LABEL_WIDTH + trackWidth;
    const timeZone = data.timezone || undefined;
    const zoomPct = Math.round(zoom * 100);

    const viewportLabel = viewportHours >= 168 ? "7 days" : `${Math.round(viewportHours)}h view`;

    const axis = renderTimeAxis(rangeStartMs, rangeEndMs, pxPerMinute, timeZone);
    const systemTrack = renderLinearTrack(
      data.system.segments,
      data.markers,
      rangeStartMs,
      rangeEndMs,
      pxPerMinute,
      timeZone,
      zoom
    );

    const systemToggle = `<button type="button" class="tl-toggle" data-toggle="system" aria-expanded="${expanded.system ? "true" : "false"}">${expanded.system ? "▼" : "▶"}</button>`;
    let rows = renderRow(data.system.label, systemTrack, systemToggle, "tl-row-system", trackWidth);

    if (expanded.system) {
      for (const device of data.devices || []) {
        const devMarkers = (data.markers || []).filter((m) =>
          device.switches.some((s) => s.name === m.pump_name)
        );
        const devTrack = renderLinearTrack(
          device.segments,
          devMarkers,
          rangeStartMs,
          rangeEndMs,
          pxPerMinute,
          timeZone,
          zoom
        );
        let devToggle = "";
        if (device.expandable) {
          const isOpen = !!expanded.devices[device.key];
          devToggle = `<button type="button" class="tl-toggle" data-toggle="device" data-device-key="${escapeAttr(device.key)}" aria-expanded="${isOpen ? "true" : "false"}">${isOpen ? "▼" : "▶"}</button>`;
        } else {
          devToggle = `<span class="tl-toggle-spacer"></span>`;
        }
        rows += renderRow(device.label, devTrack, devToggle, "tl-row-device", trackWidth);

        if (device.expandable && expanded.devices[device.key]) {
          for (const sw of device.switches) {
            const swMarkers = (data.markers || []).filter((m) => m.pump_name === sw.name);
            const swTrack = renderLinearTrack(
              sw.segments,
              swMarkers,
              rangeStartMs,
              rangeEndMs,
              pxPerMinute,
              timeZone,
              zoom
            );
            rows += renderRow(
              sw.label,
              swTrack,
              `<span class="tl-toggle-spacer"></span>`,
              "tl-row-switch",
              trackWidth
            );
          }
        }
      }
    }

    return `
      <div class="tl-meta">
        <span>${escapeAttr(data.range_start_local)} → ${escapeAttr(data.range_end_local)} (data: 7 days · ${escapeAttr(viewportLabel)})</span>
        <span class="tl-meta-hint">All times are ${escapeAttr(data.timezone || "local")} · same as Recent Events table</span>
        <div class="tl-scroll-nav">
          <button type="button" class="tl-scroll-btn tl-view-btn" id="history-view-24h" title="Fit last 24 hours">24h</button>
          <button type="button" class="tl-scroll-btn tl-view-btn" id="history-view-7d" title="Fit full 7 days">7d</button>
          <button type="button" class="tl-scroll-btn" id="history-zoom-out" title="Zoom out">−</button>
          <span class="tl-zoom-label" id="history-zoom-label">${zoomPct}%</span>
          <button type="button" class="tl-scroll-btn" id="history-zoom-in" title="Zoom in">+</button>
          <button type="button" class="tl-scroll-btn" id="history-scroll-left" title="Scroll older">← Older</button>
          <button type="button" class="tl-scroll-btn" id="history-scroll-right" title="Scroll to Now">Now →</button>
          <button type="button" class="tl-refresh" id="history-timeline-refresh">Refresh</button>
        </div>
      </div>
      <div class="tl-scroll" id="history-timeline-scroll">
        <div class="tl-scroll-inner" style="min-width:${innerWidth}px">
          <div class="tl-axis-row" style="min-width:${innerWidth}px">
            <div class="tl-label tl-axis-label" style="width:${LABEL_WIDTH}px">Timeline</div>
            <div class="tl-track-wrap tl-axis-wrap" style="width:${trackWidth}px">${axis}</div>
          </div>
          ${rows}
        </div>
      </div>
      <div class="tl-legend">
        <span><i class="tl-legend-swatch tl-on"></i> ON</span>
        <span><i class="tl-legend-swatch tl-off"></i> OFF</span>
        <span><i class="tl-legend-swatch tl-gap"></i> idle (…)</span>
        <span><i class="tl-marker tl-marker-on tl-legend-marker"></i> turn on</span>
        <span><i class="tl-marker tl-marker-off tl-legend-marker"></i> turn off</span>
        <span><i class="tl-marker tl-marker-on failed tl-legend-marker"></i> failed command</span>
      </div>
      ${renderRecentEvents(data.markers, timeZone, data.timezone)}`;
  }

  function scrollToNow(scrollEl) {
    if (!scrollEl) return;
    requestAnimationFrame(() => {
      scrollEl.scrollLeft = scrollEl.scrollWidth - scrollEl.clientWidth;
    });
  }

  function scrollToTime(tsMs, rangeStartMs, pxPerMinute, scrollEl) {
    if (!scrollEl) return;
    const x = msToX(tsMs, rangeStartMs, pxPerMinute);
    requestAnimationFrame(() => {
      scrollEl.scrollLeft = Math.max(0, x - scrollEl.clientWidth * 0.35);
    });
  }

  function bindTimeline(root, data) {
    let expanded = loadExpandedState();
    if (typeof expanded.system !== "boolean") expanded.system = false;
    if (!expanded.devices) expanded.devices = {};
    let zoom = loadZoom();
    let viewportHours = loadViewportHours();

    function paint(scrollMode) {
      const scrollEl = root.querySelector("#history-timeline-scroll");
      const prevRatio =
        scrollEl && scrollEl.scrollWidth > scrollEl.clientWidth
          ? scrollEl.scrollLeft / (scrollEl.scrollWidth - scrollEl.clientWidth)
          : 1;

      if (scrollMode === "fitViewport") {
        zoom = fitZoomForViewportHours(viewportHours, scrollEl);
        saveZoom(zoom);
      }

      root.innerHTML = renderTimeline(data, expanded, zoom, viewportHours);
      let newScrollEl = root.querySelector("#history-timeline-scroll");

      if (scrollMode === "fitViewport" && newScrollEl) {
        const fitted = fitZoomForViewportHours(viewportHours, newScrollEl);
        if (Math.abs(fitted - zoom) > 0.02) {
          zoom = fitted;
          saveZoom(zoom);
          root.innerHTML = renderTimeline(data, expanded, zoom, viewportHours);
          newScrollEl = root.querySelector("#history-timeline-scroll");
        }
      }

      const rangeStartMs = parseMs(data.range_start);
      const pxPerMinute = BASE_PX_PER_MINUTE * zoom;

      if (scrollMode === "event" && scrollModeTs != null && newScrollEl) {
        scrollToTime(scrollModeTs, rangeStartMs, pxPerMinute, newScrollEl);
      } else if (scrollMode === "preserve" && newScrollEl) {
        requestAnimationFrame(() => {
          const max = newScrollEl.scrollWidth - newScrollEl.clientWidth;
          newScrollEl.scrollLeft = max > 0 ? prevRatio * max : 0;
        });
      } else {
        scrollToNow(newScrollEl);
      }

      bindHandlers();
    }

    let scrollModeTs = null;

    function bindHandlers() {
      root.querySelectorAll('[data-toggle="system"]').forEach((btn) => {
        btn.onclick = () => {
          expanded.system = !expanded.system;
          saveExpandedState(expanded);
          paint("preserve");
        };
      });
      root.querySelectorAll('[data-toggle="device"]').forEach((btn) => {
        btn.onclick = () => {
          const key = btn.getAttribute("data-device-key");
          if (!key) return;
          expanded.devices[key] = !expanded.devices[key];
          saveExpandedState(expanded);
          paint("preserve");
        };
      });

      const refreshBtn = root.querySelector("#history-timeline-refresh");
      if (refreshBtn) refreshBtn.onclick = () => loadTimeline(root);

      const scrollEl = root.querySelector("#history-timeline-scroll");
      const scrollLeftBtn = root.querySelector("#history-scroll-left");
      const scrollRightBtn = root.querySelector("#history-scroll-right");
      const zoomInBtn = root.querySelector("#history-zoom-in");
      const zoomOutBtn = root.querySelector("#history-zoom-out");
      const view24Btn = root.querySelector("#history-view-24h");
      const view7dBtn = root.querySelector("#history-view-7d");

      root.querySelectorAll(".tl-event-jump").forEach((btn) => {
        btn.onclick = () => {
          const ts = btn.getAttribute("data-ts");
          if (!ts) return;
          scrollModeTs = parseMs(ts);
          paint("event");
          scrollModeTs = null;
        };
      });

      if (view24Btn) {
        view24Btn.onclick = () => {
          viewportHours = 24;
          saveViewportHours(viewportHours);
          paint("fitViewport");
        };
      }
      if (view7dBtn) {
        view7dBtn.onclick = () => {
          viewportHours = 168;
          saveViewportHours(viewportHours);
          paint("fitViewport");
        };
      }

      if (scrollEl && scrollLeftBtn) {
        scrollLeftBtn.onclick = () => {
          scrollEl.scrollBy({ left: -Math.max(280, scrollEl.clientWidth * 0.55), behavior: "smooth" });
        };
      }
      if (scrollEl && scrollRightBtn) {
        scrollRightBtn.onclick = () => scrollToNow(scrollEl);
      }
      if (zoomInBtn) {
        zoomInBtn.onclick = () => {
          zoom = Math.min(12, zoom * 1.35);
          saveZoom(zoom);
          paint("preserve");
        };
      }
      if (zoomOutBtn) {
        zoomOutBtn.onclick = () => {
          zoom = Math.max(0.2, zoom / 1.35);
          saveZoom(zoom);
          paint("preserve");
        };
      }

      if (scrollEl) {
        scrollEl.onwheel = (event) => {
          if (event.shiftKey) {
            event.preventDefault();
            scrollEl.scrollLeft += event.deltaY;
          }
        };
      }
    }

    paint("fitViewport");
  }

  async function loadTimeline(root) {
    root.innerHTML = '<div class="tl-loading">Loading timeline…</div>';
    try {
      const resp = await fetch(
        `/api/history/timeline?hours=${HOURS_DEFAULT}&idle_gap_minutes=60`
      );
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      bindTimeline(root, data);
    } catch (err) {
      root.innerHTML = `<div class="tl-error">Failed to load timeline: ${escapeAttr(err.message || err)}</div>`;
    }
  }

  window.initHistoryTimeline = function initHistoryTimeline(rootEl) {
    if (!rootEl) return;
    loadTimeline(rootEl);
  };
})();
