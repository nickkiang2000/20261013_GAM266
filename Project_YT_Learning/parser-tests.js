// Run with: node parser-tests.js
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(__dirname + '/sidepanel.js','utf8').replace(/\nloadData\(\);\s*$/, '');
const context = { document: { getElementById: () => ({ addEventListener(){}, textContent:'', innerHTML:'', className:'', append(){}, value:'' }) },
  chrome: { tabs:{query(){}}, storage:{local:{get:async()=>({}),set:async()=>{}},onChanged:{addListener(){}}}, runtime:{onMessage:{addListener(){},lastError:null} } },
  Blob, URL, setTimeout, console };
vm.createContext(context);
vm.runInContext(source + `
;globalThis.__parseTranscript=parseTranscript;
;globalThis.__parseYouTubeCopiedLine=parseYouTubeCopiedLine;
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
  language:'ja',title:'東京とOpenAI',analysisSegments:[
    {start:0,end:10,text:'東京でOpenAIとGPTについて話します。ソウルにも行きます。',sourceSegmentIds:['tr-1']},
    {start:12,end:20,text:'東京ではGPTを使いました。',sourceSegmentIds:['tr-2']}
  ]
});
for (const expected of ['東京','OpenAI','GPT','ソウル']) {
  if (!terms.some(term => term.term === expected)) throw new Error('Term extraction missing: '+expected);
}
const tokyo = terms.find(term => term.term === '東京');
if (tokyo.frequency < 2 || tokyo.occurrences.length !== 2) throw new Error('Term occurrence mapping failed');
console.log("Parser tests passed.");
