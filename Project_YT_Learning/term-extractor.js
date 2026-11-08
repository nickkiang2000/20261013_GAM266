(() => {
  const STOPWORDS = {
    en: new Set("the a an and or but if then this that these those it its we you they he she i me my our your their is are was were be been being have has had do does did can could would should will just very really today now here there video people thing things time one two like get got going go make made see know want think because so also about with from into for of on in at to as by".split(" ")),
    ja: new Set("これ それ あれ ここ そこ ため よう もの こと ところ いま 今日 今回 自分 私 僕 人 方 時 感じ 本当 動画 最後 最初 ちょっと とても そして しかし だから です ます いる ある する なる できる 思う 見る 行く 来る 言う".split(" ")),
    zh: new Set("這個 那個 這些 那些 這裡 那裡 今天 現在 這次 自己 我們 你們 他們 大家 事情 東西 時候 感覺 影片 視頻 開始 最後 然後 但是 因為 所以 可以 可能 就是 還有 一個 一些 什麼 怎麼 這樣 那樣".split(" "))
  };

  const normalize = value => String(value || "").replace(/\s+/g, " ").trim().normalize("NFKC");
  const languageFamily = language => /^ja/i.test(language || "") ? "ja" : /^zh/i.test(language || "") ? "zh" : "en";

  function tokens(text, language) {
    try {
      const segmenter = new Intl.Segmenter(language || "en", { granularity: "word" });
      return [...segmenter.segment(text)].filter(item => item.isWordLike).map(item => normalize(item.segment));
    } catch (_) {
      return normalize(text).match(/[\p{L}\p{M}\p{N}][\p{L}\p{M}\p{N}'’.&+_-]*/gu) || [];
    }
  }

  function candidateType(term) {
    if (/^[A-Z][A-Z0-9.&+-]{1,}$/.test(term)) return "acronym";
    if (/^[\p{Script=Katakana}ー]{3,}$/u.test(term)) return "katakana_name";
    if (/^[A-Z][\p{L}\p{M}'’-]+(?:\s+[A-Z][\p{L}\p{M}'’-]+)+$/u.test(term)) return "proper_name";
    if (/\p{Script=Han}/u.test(term)) return "cjk_term";
    if (/[A-Z]/.test(term) && /\d|[a-z]/.test(term)) return "product_or_brand";
    return "term";
  }

  function patternConfidence(term, type, family) {
    if (type === "acronym") return 0.96;
    if (type === "proper_name") return 0.92;
    if (type === "product_or_brand") return 0.84;
    if (type === "katakana_name") return 0.78;
    if (type === "cjk_term") return family === "ja" ? 0.52 : 0.58;
    return 0.38;
  }

  function addCandidate(map, rawTerm, segment, family, title) {
    const term = normalize(rawTerm).replace(/^[\s.,!?「」『』【】（）()]+|[\s.,!?「」『』【】（）()]+$/g, "");
    if (term.length < 2 || term.length > 48 || /^\d+(?:[.,:]\d+)*$/.test(term)) return;
    const key = /[A-Za-z]/.test(term) ? term.toLocaleLowerCase("en") : term;
    if (STOPWORDS[family].has(key) || STOPWORDS[family].has(term)) return;
    const type = candidateType(term);
    const confidence = patternConfidence(term, type, family);
    const titleMatch = normalize(title).toLocaleLowerCase().includes(key);
    let item = map.get(key);
    if (!item) {
      item = { term, normalizedTerm: key, type, patternConfidence: confidence, frequency: 0, segmentIndexes: new Set(), occurrences: [], titleMatch };
      map.set(key, item);
    }
    item.frequency++;
    if (!item.segmentIndexes.has(segment.index)) {
      item.segmentIndexes.add(segment.index);
      item.occurrences.push({
        start: segment.start,
        end: segment.end,
        text: normalize(segment.text).slice(0, 240),
        sourceSegmentIds: segment.sourceSegmentIds || []
      });
    }
  }

  function extractTerms({ analysisSegments = [], title = "", language = "en", limit = 80 } = {}) {
    const family = languageFamily(language);
    const map = new Map();
    const prepared = analysisSegments.map((segment, index) => ({ ...segment, index }));
    for (const segment of prepared) {
      const text = normalize(segment.text);
      for (const match of text.matchAll(/\b[A-Z][A-Z0-9.&+-]{1,}\b/g)) addCandidate(map, match[0], segment, family, title);
      for (const match of text.matchAll(/\b[A-Z][\p{L}\p{M}'’-]{1,}(?:\s+[A-Z][\p{L}\p{M}'’-]{1,}){0,3}/gu)) addCandidate(map, match[0], segment, family, title);
      for (const match of text.matchAll(/[\p{Script=Katakana}ー]{3,}/gu)) addCandidate(map, match[0], segment, family, title);
      for (const token of tokens(text, language)) {
        const containsHan = /\p{Script=Han}/u.test(token);
        const mixedBrand = /[A-Z]/.test(token) && /[a-z0-9]/.test(token);
        if ((family !== "en" && containsHan && token.length >= 2) || mixedBrand) addCandidate(map, token, segment, family, title);
      }
    }

    const totalSegments = Math.max(1, prepared.length);
    return [...map.values()].map(item => {
      const segmentCount = item.segmentIndexes.size;
      const frequencyScore = Math.min(1, Math.log1p(item.frequency) / Math.log(8));
      const dispersionScore = Math.min(1, segmentCount / Math.min(6, totalSegments));
      const lengthScore = Math.min(1, [...item.term].length / 8);
      const localScore = 100 * (
        item.patternConfidence * 0.40 +
        frequencyScore * 0.25 +
        dispersionScore * 0.20 +
        (item.titleMatch ? 1 : 0) * 0.10 +
        lengthScore * 0.05
      );
      return {
        term: item.term,
        normalizedTerm: item.normalizedTerm,
        type: item.type,
        localScore: Number(localScore.toFixed(2)),
        score: Number(localScore.toFixed(2)),
        frequency: item.frequency,
        segmentCount,
        titleMatch: item.titleMatch,
        patternConfidence: item.patternConfidence,
        occurrences: item.occurrences.slice(0, 12),
        rankingSignals: { frequencyScore, dispersionScore, titleMatch: item.titleMatch, patternConfidence: item.patternConfidence }
      };
    }).filter(item =>
      item.patternConfidence >= 0.75 || item.titleMatch || item.frequency >= 2 || item.segmentCount >= 2
    ).sort((a, b) => b.score - a.score || b.segmentCount - a.segmentCount || a.term.localeCompare(b.term)).slice(0, limit);
  }

  globalThis.YTKITermExtractor = { extractTerms, languageFamily };
})();
