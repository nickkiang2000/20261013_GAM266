(() => {
  const snapshot = [
    ["TWSE","2330","台積電",["台積電","台積","TSMC"],"晶圓代工及先進封裝。","www.tsmc.com","www.tsmc.com/english/aboutTSMC"],
    ["TWSE","3045","台灣大哥大",["台灣大哥大","臺灣大哥大","台灣大"],"提供行動通訊、寬頻及數位服務，業務亦涵蓋電子商務。","corp.taiwanmobile.com","corp.taiwanmobile.com/company-profile/company-profile.html"],
    ["TWSE","2454","聯發科",["聯發科","联发科","MediaTek"],"設計行動通訊、智慧裝置與連網晶片。","www.mediatek.com","www.mediatek.com/about"],
    ["TWSE","2317","鴻海",["鴻海","鸿海","Foxconn"],"提供電子產品設計與製造服務。","www.honhai.com","www.honhai.com/en-us/about/group-profile"],
    ["TWSE","2412","中華電信",["中華電信","中华电信","Chunghwa Telecom"],"提供行動、固網寬頻與企業資通訊服務。","www.cht.com.tw","www.cht.com.tw/en/home/cht/about-cht/company-profile"],
    ["Nasdaq","AAPL","Apple",["蘋果公司","苹果公司","Apple Inc."],"設計 iPhone、Mac 等裝置，並營運數位服務。","www.apple.com","investor.apple.com",320193],
    ["Nasdaq","NVDA","NVIDIA",["輝達","英偉達","英伟达","NVIDIA"],"設計 GPU、加速運算平台與相關軟體。","www.nvidia.com","investor.nvidia.com",1045810],
    ["Nasdaq","MSFT","Microsoft",["微軟","微软","Microsoft"],"提供企業軟體、雲端運算與生產力工具。","www.microsoft.com","www.microsoft.com/en-us/Investor/",789019],
    ["Nasdaq","AMD","AMD",["超微半導體","Advanced Micro Devices"],"設計處理器、顯示晶片及資料中心加速器。","www.amd.com","ir.amd.com",2488],
    ["Nasdaq","AMZN","Amazon",["亞馬遜","亚马逊","Amazon.com"],"經營電子商務及 AWS 雲端運算。","www.amazon.com","www.aboutamazon.com/about-us",1018724],
    ["Nasdaq","GOOGL","Alphabet A 股",["Alphabet Class A"],"Google 的母公司，業務包括搜尋、廣告與雲端服務。","abc.xyz","abc.xyz/investor/",1652044]
  ].map(([market, symbol, name, aliases, description, website, profile, cik]) => ({
    market, symbol, name, aliases, description,
    website: `https://${website}`,
    profileUrl: `https://${profile}`,
    url: cik ? `https://www.sec.gov/edgar/browse/?CIK=${cik}` : null
  }));
  const getSnapshot = () => snapshot;

  function match(rows, segments) {
    const evidence = new Map();
    for (const cue of segments) {
      const line = String(cue.text || "").normalize("NFKC");
      const qualified = /股票|股價|代號|台股|美股|上市|上櫃|\b(?:ticker|stock|shares|nasdaq|nyse)\b/i.test(line);
      const tokens = new Set(line.match(/(?<![A-Za-z0-9])\$?[A-Z]{1,6}(?:[.-][A-Z])?(?![A-Za-z0-9])|(?<!\d)\d{4}(?!\d)/g) || []);
      const lower = line.toLowerCase();
      for (const row of rows) {
        const alias = [row.name, ...(row.aliases || [])].find(name => {
          if (!name || name.length < 2) return false;
          if (/\p{Script=Han}/u.test(name)) return lower.includes(name.toLowerCase());
          if (["NVIDIA", "Microsoft", "MediaTek", "Foxconn", "Amazon.com"].includes(name)) {
            return lower.includes(name.toLowerCase());
          }
          return name.includes(" ") && lower.includes(name.toLowerCase());
        });
        const explicit = tokens.has("$" + row.symbol) || (qualified && tokens.has(row.symbol));
        if (!alias && !explicit) continue;
        if (!evidence.has(row)) evidence.set(row, new Set());
        if (alias) evidence.get(row).add(alias);
        if (explicit) evidence.get(row).add(row.symbol);
      }
    }
    return [...evidence].map(([row, terms]) => ({...row, terms:[...terms]}))
      .sort((a,b) => b.terms.length - a.terms.length);
  }
  globalThis.YTKITickers = {getSnapshot, match};
})();
