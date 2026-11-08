(() => {
  const normalize = value => String(value || "").replace(/\s+/g, " ").trim().normalize("NFKC");
  const languageFamily = language => /^ja/i.test(language || "") ? "ja" : /^zh/i.test(language || "") ? "zh" : "en";

  // High-precision seed lexicon. It deliberately contains named people,
  // places, peoples, works and established technical concepts rather than
  // general vocabulary. It can grow independently of the extraction rules.
  const ENTITY_LEXICON = [
    ["贾雷德·戴蒙德", "person", "賈雷德·戴蒙德", "Jared Diamond"],
    ["枪炮、病菌与钢铁", "work", "槍炮、病菌與鋼鐵", "Guns, Germs, and Steel"],
    ["地理决定论", "theory", "地理決定論"], ["大崩坏", "work", "大崩壞", "Collapse"],
    ["马克思", "person", "馬克思", "Karl Marx"], ["奥卡姆剃刀", "concept", "奧卡姆剃刀", "Occam's razor"],
    ["达尔文演化论", "theory", "達爾文演化論"], ["燃素说", "theory", "燃素說", "phlogiston theory"],
    ["爱因斯坦", "person", "愛因斯坦", "Albert Einstein"], ["量子力学", "field", "量子力學", "quantum mechanics"],
    ["牛顿", "person", "牛頓", "Isaac Newton"], ["牛顿力学", "field", "牛頓力學", "Newtonian mechanics"],
    ["爱丁顿 (Arthur Eddington)", "person", "爱丁顿", "愛丁頓", "Arthur Eddington"],
    ["日食实验", "experiment", "日食實驗", "Eddington experiment"],
    ["三峡", "place", "三峽", "Three Gorges"], ["长江", "place", "長江", "Yangtze River"],
    ["太平洋", "place", "Pacific Ocean"], ["洞庭湖", "place"], ["黄河", "place", "黃河", "Yellow River"],
    ["渤海", "place"], ["印度", "place", "India"], ["格陵兰岛", "place", "格陵蘭島", "Greenland"],
    ["维京人", "people", "維京人", "Vikings"], ["挪威", "place", "Norway"],
    ["雅利安", "people", "Aryan"], ["欧亚大草原", "place", "歐亞大草原", "Eurasian Steppe"],
    ["吉尔吉斯人", "people", "吉爾吉斯人", "Kyrgyz people"], ["东方学家", "occupation", "東方學家", "Orientalist"]
  ].map(([label, type, ...aliases]) => ({ label, type, aliases: [label, ...aliases] }));
  const ALIAS_LOOKUP = new Map();
  for (const entity of ENTITY_LEXICON) for (const alias of entity.aliases)
    ALIAS_LOOKUP.set(normalize(alias).toLocaleLowerCase(), entity);

  const GENERIC_TERMS = new Set([
    "文明","馴化","驯化","力学","理論","理论","實驗","实验","學說","学说","主義","主义",
    "社會","社会","結構","结构","資源","资源","物理","地理","科學","科学","東方人","东方人",
    "西方人","非洲人","美洲人","人類","人类","世界","國家","国家","問題","问题","方法","原因"
  ]);
  const ACADEMIC_SUFFIX = /(?:決定論|决定论|演化論|演化论|進化論|进化论|量子力學|量子力学|牛頓力學|牛顿力学|相對論|相对论|剃刀|定律|定理|效應|效应|悖論|悖论|假說|假说|學說|学说|主義|主义|實驗|实验)$/u;

  function typeForPattern(term) {
    if (/^[A-Z][A-Z0-9.&+-]{1,}$/.test(term)) return "acronym";
    if (/^[\p{Script=Katakana}ー]{3,}$/u.test(term)) return "katakana_name";
    if (/^[A-Z][\p{L}\p{M}'’-]+(?:\s+[A-Z][\p{L}\p{M}'’-]+)+$/u.test(term)) return "proper_name";
    if (/^[\p{Script=Han}]{1,10}·[\p{Script=Han}·]{1,16}$/u.test(term)) return "person";
    if (ACADEMIC_SUFFIX.test(term)) return "academic_concept";
    return "named_entity";
  }

  function addCandidate(map, rawTerm, segment, title, options = {}) {
    const term = normalize(rawTerm).replace(/^[\s.,!?「」『』【】（）()]+|[\s.,!?「」『』【】（）()]+$/g, "");
    if (term.length < 2 || term.length > 64 || GENERIC_TERMS.has(term) || /^\d+(?:[.,:]\d+)*$/.test(term)) return;
    const knownEntity = ALIAS_LOOKUP.get(term.toLocaleLowerCase());
    const key = options.canonicalKey || (knownEntity ? `lexicon:${knownEntity.label}` : (/\p{Script=Latin}/u.test(term) ? term.toLocaleLowerCase("en") : term));
    if (knownEntity) options = { displayTerm: knownEntity.label, type: knownEntity.type, confidence: 0.99, signal: options.signal || "curated_entity_alias", ...options };
    let item = map.get(key);
    if (!item) {
      item = {
        term: options.displayTerm || term, normalizedTerm: key,
        type: options.type || typeForPattern(term), patternConfidence: options.confidence || 0.9,
        lookupTerms: options.lookupTerms || [term],
        signals: new Set(), frequency: 0, segmentIndexes: new Set(), occurrences: [],
        titleMatch: normalize(title).toLocaleLowerCase().includes(term.toLocaleLowerCase())
      };
      map.set(key, item);
    }
    item.signals.add(options.signal || "format_rule");
    item.frequency++;
    if (!item.segmentIndexes.has(segment.index)) {
      item.segmentIndexes.add(segment.index);
      item.occurrences.push({ start: segment.start, end: segment.end, text: normalize(segment.text).slice(0, 260), sourceSegmentIds: segment.sourceSegmentIds || [] });
    }
  }

  function extractTerms({ analysisSegments = [], title = "", language = "en", limit = 120 } = {}) {
    const map = new Map();
    const prepared = analysisSegments.map((segment, index) => ({ ...segment, index }));
    for (const segment of prepared) {
      const text = normalize(segment.text);
      for (const entity of ENTITY_LEXICON) {
        const matched = entity.aliases.filter(alias => alias && text.toLocaleLowerCase().includes(alias.toLocaleLowerCase()));
        if (!matched.length) continue;
        addCandidate(map, matched[0], segment, title, {
          canonicalKey: `lexicon:${entity.label}`, displayTerm: entity.label, type: entity.type,
          confidence: 0.99, signal: "curated_entity_lexicon", lookupTerms: entity.aliases
        });
      }
      for (const match of text.matchAll(/[《〈「『【]([^》〉」』】]{2,50})[》〉」』】]/gu))
        addCandidate(map, match[1], segment, title, { type: "work", confidence: 0.98, signal: "quoted_title" });
      for (const match of text.matchAll(/[\p{Script=Han}]{1,10}·[\p{Script=Han}·]{1,16}/gu))
        addCandidate(map, match[0].replace(/(?:寫了|写了|表示|認為|认为|說|说|的)+$/u, ""), segment, title, { type: "person", confidence: 0.98, signal: "middle_dot_name" });
      for (const match of text.matchAll(/\b[A-Z][\p{L}\p{M}'’-]{1,}(?:\s+[A-Z][\p{L}\p{M}'’-]{1,}){1,4}\b/gu))
        addCandidate(map, match[0], segment, title, { type: "proper_name", confidence: 0.95, signal: "capitalized_name" });
      for (const match of text.matchAll(/\b[A-Z][A-Z0-9.&+-]{1,}\b/g))
        addCandidate(map, match[0], segment, title, { type: "acronym", confidence: 0.96, signal: "acronym" });
      for (const match of text.matchAll(/[\p{Script=Katakana}ー]{3,}/gu))
        addCandidate(map, match[0], segment, title, { type: "katakana_name", confidence: 0.86, signal: "katakana_name" });

      // Plain repeated Chinese words no longer qualify. Academic phrases need
      // a recognized suffix and a compact, punctuation-bounded phrase.
      const clauses = text.split(/[，,。！？!?；;：:\n]|(?:所謂|所谓|叫做|稱為|称为|提出|談到|谈到|講到|讲到)/u);
      for (const clause of clauses) {
        const words = clause.match(/[\p{Script=Han}]{2,16}/gu) || [];
        for (const word of words) {
          const phrase=word.replace(/^(?:的|討論|讨论|關於|关于|研究|解釋|解释)+/u, "").split(/[與与和及]/u).pop();
          if (ACADEMIC_SUFFIX.test(phrase) && phrase.length >= 3 && phrase.length <= 12)
            addCandidate(map, phrase, segment, title, { type: "academic_concept", confidence: 0.88, signal: "academic_suffix" });
        }
      }
    }

    return [...map.values()].map(item => {
      const segmentCount = item.segmentIndexes.size;
      const frequencyScore = Math.min(1, Math.log1p(item.frequency) / Math.log(6));
      const dispersionScore = Math.min(1, segmentCount / 4);
      const score = 100 * (item.patternConfidence * 0.72 + frequencyScore * 0.13 + dispersionScore * 0.10 + (item.titleMatch ? 0.05 : 0));
      return {
        term: item.term, normalizedTerm: item.normalizedTerm, type: item.type,
        lookupTerms: item.lookupTerms,
        localScore: Number(score.toFixed(2)), score: Number(score.toFixed(2)), frequency: item.frequency,
        segmentCount, titleMatch: item.titleMatch, patternConfidence: item.patternConfidence,
        extractionSignals: [...item.signals], occurrences: item.occurrences,
        rankingSignals: { frequencyScore, dispersionScore, titleMatch: item.titleMatch, patternConfidence: item.patternConfidence }
      };
    }).sort((a, b) => b.score - a.score || b.segmentCount - a.segmentCount || a.term.localeCompare(b.term)).slice(0, limit);
  }

  globalThis.YTKITermExtractor = { extractTerms, languageFamily };
})();
