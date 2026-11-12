// Run with: node parser-tests.js
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(__dirname + '/sidepanel.js','utf8').replace(/\nloadData\(\);\s*$/, '');
const context = { document: { getElementById: () => ({ addEventListener(){}, textContent:'', innerHTML:'', className:'', append(){}, value:'' }) },
  chrome: { tabs:{query(){}}, storage:{local:{get:async()=>({}),set:async()=>{}},onChanged:{addListener(){}}}, runtime:{onMessage:{addListener(){},lastError:null} } },
  Blob, URL, setTimeout, setInterval:()=>0, console };
vm.createContext(context);
vm.runInContext(source + `
;globalThis.__parseTranscript=parseTranscript;
;globalThis.__parseYouTubeCopiedLine=parseYouTubeCopiedLine;
;globalThis.__refineOccurrencesFromRawCues=refineOccurrencesFromRawCues;
`, context);
const cases = [
  ["3:103 分 10 秒字幕文字", 190, "字幕文字"],
  ["0:044 秒こんにちは。", 4, "こんにちは。"],
  ["1:041 分 4 秒こんにちは", 64, "こんにちは"],
  ["10:0210 分 2 秒字幕內容", 602, "字幕內容"],
  ["39:5039 分 50 秒[音楽]", 2390, "[音楽]"]
];
for (const [input, seconds, text] of cases) {
  const parsed = context.__parseYouTubeCopiedLine(input);
  if (!parsed || parsed.start !== seconds || parsed.text !== text) throw new Error("Failed: " + input + " => " + JSON.stringify(parsed));
}
const srt = "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:02,000 --> 00:00:03,000\nWorld";
if (context.__parseTranscript(srt).length !== 2) throw new Error("SRT test failed");

const utilsSource = fs.readFileSync(__dirname + '/transcript-utils.js','utf8');
const utilsContext = {};
vm.createContext(utilsContext);
vm.runInContext(utilsSource, utilsContext);
const rawSegments = [
  {id:'tr-1',start:0,end:2,duration:2,text:'Alpha'},
  {id:'tr-2',start:2.5,end:5,duration:2.5,text:'Beta'},
  {id:'tr-3',start:40,end:null,duration:null,text:'Gamma'}
];
const analysis = utilsContext.YTKITranscriptUtils.buildAnalysisSegments(rawSegments);
if (analysis.length !== 2 || analysis[0].text !== 'Alpha Beta' || analysis[1].start !== 40) throw new Error('Analysis segment grouping failed');
if (analysis[1].end !== 40 || analysis[1].duration !== 0) throw new Error('Null timing normalization failed');
const plainText = utilsContext.YTKITranscriptUtils.buildPlainText(rawSegments);
const stats = utilsContext.YTKITranscriptUtils.transcriptStats(rawSegments, analysis, plainText);
if (plainText !== 'Alpha\nBeta\nGamma' || stats.rawSegmentCount !== 3 || stats.analysisSegmentCount !== 2) throw new Error('Transcript derived data failed');

const termSource = fs.readFileSync(__dirname + '/term-extractor.js','utf8');
const termContext = {};
vm.createContext(termContext);
vm.runInContext(termSource,termContext);
const terms = termContext.YTKITermExtractor.extractTerms({
  language:'zh',title:'缺少土豪的世界',analysisSegments:[
    {start:0,end:30,text:'贾雷德·戴蒙德写了《枪炮、病菌与钢铁》，讨论地理决定论。文明、馴化、事情和方法都只是一般用字。',sourceSegmentIds:['tr-1']},
    {start:31,end:60,text:'马克思、奥卡姆剃刀、达尔文演化论、燃素说、爱因斯坦、量子力学与牛顿力学。',sourceSegmentIds:['tr-2']},
    {start:61,end:90,text:'爱丁顿 Arthur Eddington 的日食实验；三峡、长江、太平洋、洞庭湖、黄河、渤海和印度。',sourceSegmentIds:['tr-3']},
    {start:91,end:120,text:'格陵兰岛的维京人来自挪威，另有雅利安、欧亚大草原、吉尔吉斯人与东方学家。',sourceSegmentIds:['tr-4']},
    {start:121,end:150,text:'在前幾年經常出賈雷德.戴蒙德的書，但是我覺得賈雷德.戴蒙德太注重物理性的一些原因。包括達爾文演化論，那時候牛頓力學。',sourceSegmentIds:['tr-5']}
  ]
});
const expectedTerms=['贾雷德·戴蒙德','枪炮、病菌与钢铁','地理决定论','马克思','奥卡姆剃刀','达尔文演化论','燃素说','爱因斯坦','量子力学','牛顿力学','爱丁顿 (Arthur Eddington)','日食实验','三峡','长江','太平洋','洞庭湖','黄河','渤海','印度','格陵兰岛','维京人','挪威','雅利安','欧亚大草原','吉尔吉斯人','东方学家'];
for (const expected of expectedTerms) if (!terms.some(term => term.term === expected)) throw new Error('High-precision term extraction missing: '+expected);
for (const rejected of ['文明','馴化','事情','方法','力学']) if (terms.some(term => term.term === rejected)) throw new Error('Generic term was not rejected: '+rejected);
for (const rejected of ['在前幾年經常出賈雷德.戴蒙德的書','但是我覺得賈雷德.戴蒙德太注重物理性的一些原因','包括達爾文演化論','那時候牛頓力學']) if (terms.some(term => term.term === rejected)) throw new Error('Sentence fragment was not rejected: '+rejected);
const diamond=terms.find(term=>term.term==='贾雷德·戴蒙德');
if (!diamond.occurrences.length || diamond.occurrences[0].start !== 0) throw new Error('Term occurrence mapping failed');

const englishTerms=termContext.YTKITermExtractor.extractTerms({language:'en',title:'Africa and Greenland',analysisSegments:[
  {start:0,end:30,text:'Sudan Congo Mozambique Nigeria Somalia and the Sahel are in Africa. Greenland is part of Denmark and GRML explores there.',sourceSegmentIds:['en-1']},
  {start:31,end:60,text:'China Russia South Africa and BRICS discuss Skaergaard platinum palladium and Sarfartoq in Europe.',sourceSegmentIds:['en-2']},
  {start:61,end:90,text:'Political scientists Robert Jackson and Carl Rosberg later wrote about post-colonial Africa, Khartoum and Darfur.',sourceSegmentIds:['en-3']},
  {start:91,end:130,text:'In Nigeria, Katanga, the United Nations, Belgium, America, The United States, Soviet Union, Cuba, France and Angola were discussed.',sourceSegmentIds:['en-4']}
]});
for(const expected of ['Sudan','Congo','Mozambique','Nigeria','Somalia','Sahel','Africa','Greenland','Denmark','GRML','China','Russia','South Africa','BRICS','Skaergaard','platinum','palladium','Sarfartoq','Europe','Robert Jackson','Carl Rosberg','post-colonial Africa','Khartoum','Darfur','Katanga','United Nations','Belgium','America','The United States','Soviet Union','Cuba','France','Angola']) if(!englishTerms.some(term=>term.term===expected))throw new Error('English entity extraction missing: '+expected);
if(englishTerms.some(term=>term.term==='In Nigeria'))throw new Error('English preposition was included in entity');
const splitCues=[
  {id:'split-1',start:16,end:18,text:'《枪炮、病菌'},
  {id:'split-2',start:18,end:21,text:'与钢铁》是一本书'}
];
const splitAnalysis=utilsContext.YTKITranscriptUtils.buildAnalysisSegments(splitCues);
const splitTerms=termContext.YTKITermExtractor.extractTerms({language:'zh',analysisSegments:splitAnalysis});
const refined=context.__refineOccurrencesFromRawCues(splitTerms,splitCues);
const splitTitle=refined.find(term=>term.term==='枪炮、病菌与钢铁');
if(!splitTitle||splitTitle.occurrences[0]?.start!==16||splitTitle.occurrences[0]?.end!==21)throw new Error('Cross-cue entity timing failed');
if(refined.filter(term=>term.term==='枪炮、病菌与钢铁').length!==1)throw new Error('Canonical and quoted entity were not deduplicated');
const translatedTerms=termContext.YTKITermExtractor.extractTerms({language:'zh-Hant',analysisSegments:[
  {start:0,end:20,text:'台灣關係 法由JD Vance和Marco Rubio討論，西亞也是重點。',sourceSegmentIds:['zh-1']}
]});
for(const expected of ['台灣關係法','JD Vance','Marco Rubio','西亞'])if(!translatedTerms.some(term=>term.term===expected))throw new Error('Translated entity normalization missing: '+expected);
const geopoliticsTerms=termContext.YTKITermExtractor.extractTerms({language:'zh-Hant',analysisSegments:[
  {start:0,end:30,text:'普丁、川普、烏克蘭與盧比歐談到中亞、中共、北京、歐盟、台灣大學、樊家忠、委內瑞拉、菲律賓、四方安全通話與一帶一路。',sourceSegmentIds:['geo-1']}
]});
for(const expected of ['普丁','川普','烏克蘭','盧比歐','中亞','中共','北京','歐盟','台灣大學','樊家忠','委內瑞拉','菲律賓','四方安全通話','一帶一路'])if(!geopoliticsTerms.some(term=>term.term===expected))throw new Error('Geopolitics entity extraction missing: '+expected);
console.log("Parser tests passed.");
