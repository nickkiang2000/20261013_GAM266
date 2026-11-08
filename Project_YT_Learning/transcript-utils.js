(() => {
  function normalizeText(value) {
    return String(value || "").replace(/\s+/g, " ").trim().normalize("NFKC");
  }

  function buildAnalysisSegments(rawSegments, options = {}) {
    const maxSeconds = Number(options.maxSeconds) || 30;
    const maxChars = Number(options.maxChars) || 480;
    const maxGapSeconds = Number(options.maxGapSeconds) || 4;
    const result = [];
    let group = null;

    const flush = () => {
      if (!group) return;
      group.id = `analysis-${result.length + 1}`;
      group.duration = group.end !== null ? Math.max(0, group.end - group.start) : null;
      result.push(group);
      group = null;
    };

    for (let index = 0; index < (rawSegments || []).length; index++) {
      const segment = rawSegments[index];
      const text = normalizeText(segment?.text);
      const start = Number(segment?.start);
      if (!text || !Number.isFinite(start)) continue;
      const endValue = segment?.end == null ? NaN : Number(segment.end);
      const durationValue = segment?.duration == null ? NaN : Number(segment.duration);
      const end = Number.isFinite(endValue)
        ? endValue
        : Number.isFinite(durationValue) ? start + durationValue : start;
      const sourceId = segment.id || `raw-${index + 1}`;

      if (!group) {
        group = { start, end, text, sourceSegmentIds: [sourceId], sourceStartIndex: index, sourceEndIndex: index };
        continue;
      }
      const gap = start - group.end;
      const combinedText = `${group.text} ${text}`;
      const combinedSpan = end - group.start;
      if (gap > maxGapSeconds || combinedSpan > maxSeconds || combinedText.length > maxChars) {
        flush();
        group = { start, end, text, sourceSegmentIds: [sourceId], sourceStartIndex: index, sourceEndIndex: index };
      } else {
        group.end = Math.max(group.end, end);
        group.text = combinedText;
        group.sourceSegmentIds.push(sourceId);
        group.sourceEndIndex = index;
      }
    }
    flush();
    return result;
  }

  function buildPlainText(rawSegments) {
    return (rawSegments || []).map(segment => normalizeText(segment?.text)).filter(Boolean).join("\n");
  }

  function transcriptStats(rawSegments, analysisSegments, plainText) {
    const first = rawSegments?.[0] || null;
    const last = rawSegments?.[rawSegments.length - 1] || null;
    return {
      rawSegmentCount: rawSegments?.length || 0,
      analysisSegmentCount: analysisSegments?.length || 0,
      characterCount: [...String(plainText || "")].length,
      firstStart: Number.isFinite(Number(first?.start)) ? Number(first.start) : null,
      lastStart: Number.isFinite(Number(last?.start)) ? Number(last.start) : null,
      lastEnd: last?.end != null && Number.isFinite(Number(last.end)) ? Number(last.end) : null
    };
  }

  globalThis.YTKITranscriptUtils = { normalizeText, buildAnalysisSegments, buildPlainText, transcriptStats };
})();
