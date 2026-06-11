const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.defineLayout({ name: "W", width: 13.333, height: 7.5 });
p.layout = "W";
const W = 13.333, H = 7.5;

// ---- Palette ----
const NAVY = "14233A", NAVY2 = "1E2C4A";
const INK = "23313F", MUTE = "6A7787";
const TEAL = "138C9B", TEALD = "0C6B78", TEALL = "E3F1F3";
const AMBER = "EF8B3B";
const RED = "CF4F4B", REDL = "FBEDEC";
const GRN = "2E9E78", GRNL = "E7F4EE";
const PANEL = "F3F7FA", LINE = "E2E8F0", WHITE = "FFFFFF";
const CJK = "PingFang TC", NUM = "Arial";

const slide = (bg) => { const s = p.addSlide(); s.background = { color: bg || WHITE }; return s; };

// kicker square + small label + big title (no underline)
function header(s, kicker, title, opt) {
  opt = opt || {};
  const cy = opt.dark ? WHITE : INK;
  s.addShape(p.ShapeType.rect, { x: 0.62, y: 0.52, w: 0.16, h: 0.16, fill: { color: TEAL } });
  s.addText(kicker, { x: 0.86, y: 0.44, w: 9, h: 0.32, fontFace: CJK, fontSize: 13, bold: true, color: TEAL, charSpacing: 2 });
  s.addText(title, { x: 0.6, y: 0.78, w: 12.1, h: 0.8, fontFace: CJK, fontSize: 29, bold: true, color: cy });
}
function pageNo(s, n) {
  s.addText(String(n).padStart(2, "0"), { x: 12.5, y: 6.92, w: 0.6, h: 0.3, fontFace: NUM, fontSize: 11, color: MUTE, align: "right" });
}
function card(s, x, y, w, h, fill, line) {
  s.addShape(p.ShapeType.roundRect, { x, y, w, h, rectRadius: 0.1, fill: { color: fill }, line: line ? { color: line, width: 1 } : { type: "none" }, shadow: { type: "outer", color: "9AA8B5", opacity: 0.28, blur: 7, offset: 2, angle: 90 } });
}

// =========================================================== 1 TITLE
(() => {
  const s = slide(NAVY);
  // route motif
  const dots = [[1.0, 5.95], [2.4, 5.55], [3.8, 5.95], [5.2, 5.5]];
  for (let i = 0; i < dots.length - 1; i++)
    s.addShape(p.ShapeType.line, { x: dots[i][0], y: dots[i][1] + 0.13, w: dots[i + 1][0] - dots[i][0], h: dots[i + 1][1] - dots[i][1], line: { color: TEAL, width: 1.5, dashType: "dash" } });
  dots.forEach((d, i) => s.addShape(p.ShapeType.ellipse, { x: d[0], y: d[1], w: 0.26, h: 0.26, fill: { color: i === dots.length - 1 ? AMBER : TEAL } }));

  s.addText("機器學習 / 強化學習 ・ 期末專題", { x: 0.9, y: 1.5, w: 11, h: 0.4, fontFace: CJK, fontSize: 15, bold: true, color: TEAL, charSpacing: 2 });
  s.addText("自主式旅遊行程排定 Agent", { x: 0.9, y: 2.05, w: 11.5, h: 1.2, fontFace: CJK, fontSize: 50, bold: true, color: WHITE });
  s.addText("一句話需求，AI 多助理協作，自動產出完整旅遊行程", { x: 0.92, y: 3.35, w: 11, h: 0.5, fontFace: CJK, fontSize: 18, color: "C7D2DF" });

  s.addShape(p.ShapeType.line, { x: 0.95, y: 4.35, w: 4.6, h: 0, line: { color: "3A4A66", width: 1 } });
  s.addText([
    { text: "第 10 組\n", options: { fontSize: 14, bold: true, color: WHITE } },
    { text: "組長　尤聖宏 M1554001　·　組員　張宇辰 M1454005\n", options: { fontSize: 13, color: "C7D2DF" } },
    { text: "github.com/SHENGHUNGYU/autonomous-itinerary-builder", options: { fontSize: 12, color: TEAL } },
  ], { x: 0.92, y: 4.5, w: 11, h: 1.2, fontFace: CJK, lineSpacingMultiple: 1.25 });
})();

// =========================================================== 2 AGENDA
(() => {
  const s = slide(WHITE);
  s.addShape(p.ShapeType.rect, { x: 0, y: 0, w: 4.5, h: H, fill: { color: NAVY } });
  s.addText("AGENDA", { x: 0.6, y: 2.5, w: 3.6, h: 0.5, fontFace: NUM, fontSize: 16, bold: true, color: TEAL, charSpacing: 3 });
  s.addText("大綱", { x: 0.6, y: 3.0, w: 3.6, h: 1, fontFace: CJK, fontSize: 40, bold: true, color: WHITE });
  const items = ["背景與動機", "目的", "方法：協作框架 / 工具", "系統架構：架構 / 分工 / 流程", "遇到的問題與解決方式", "限制", "Demo（影片）", "總結"];
  const x0 = 5.2, colW = 7.4;
  items.forEach((t, i) => {
    const y = 1.05 + i * 0.72;
    s.addShape(p.ShapeType.ellipse, { x: x0, y: y, w: 0.46, h: 0.46, fill: { color: TEALL }, line: { color: TEAL, width: 1 } });
    s.addText(String(i + 1).padStart(2, "0"), { x: x0, y: y, w: 0.46, h: 0.46, align: "center", valign: "middle", fontFace: NUM, fontSize: 13, bold: true, color: TEALD });
    s.addText(t, { x: x0 + 0.65, y: y - 0.04, w: colW - 0.7, h: 0.54, valign: "middle", fontFace: CJK, fontSize: 17, bold: true, color: INK });
  });
  pageNo(s, 2);
})();

// =========================================================== 3 背景與動機
(() => {
  const s = slide(WHITE);
  header(s, "背景與動機", "規劃多日自由行，又累又容易顧此失彼");
  s.addText([
    { text: "查景點、找美食、算每天交通會不會太趕、訂住宿、還要顧預算 —— 項目多又雜。\n", options: {} },
    { text: "網路上的行程產生器多為固定模板，無法照你「一句話」的需求量身規劃。", options: {} },
  ], { x: 0.62, y: 1.72, w: 12.1, h: 0.95, fontFace: CJK, fontSize: 15.5, color: INK, lineSpacingMultiple: 1.25 });

  s.addText("直接叫 AI 排行程，會遇到三個痛點：", { x: 0.62, y: 2.78, w: 12, h: 0.4, fontFace: CJK, fontSize: 15, bold: true, color: TEALD });
  const pains = [["編造", "會編出不存在的店家或景點"], ["算錯", "時間、預算、價格容易算錯"], ["凌亂", "輸出格式雜亂、不好直接用"]];
  pains.forEach((c, i) => {
    const x = 0.62 + i * 4.06;
    card(s, x, 3.28, 3.8, 1.55, PANEL);
    s.addShape(p.ShapeType.ellipse, { x: x + 0.28, y: 3.55, w: 0.5, h: 0.5, fill: { color: RED } });
    s.addText(String(i + 1), { x: x + 0.28, y: 3.55, w: 0.5, h: 0.5, align: "center", valign: "middle", fontFace: NUM, fontSize: 16, bold: true, color: WHITE });
    s.addText(c[0], { x: x + 0.95, y: 3.5, w: 2.7, h: 0.4, fontFace: CJK, fontSize: 17, bold: true, color: INK });
    s.addText(c[1], { x: x + 0.32, y: 4.08, w: 3.3, h: 0.6, fontFace: CJK, fontSize: 13.5, color: MUTE });
  });
  // key idea strip
  card(s, 0.62, 5.18, 12.1, 1.25, NAVY);
  s.addText("我們的想法", { x: 0.95, y: 5.4, w: 2.4, h: 0.5, fontFace: CJK, fontSize: 16, bold: true, color: TEAL });
  s.addText("讓 AI 負責「聽懂你要什麼、安排行程」，把「算時間、算錢」這種需要精準的事交給程式 —— 做出又聰明又可靠的旅遊助理。",
    { x: 3.1, y: 5.32, w: 9.4, h: 0.95, valign: "middle", fontFace: CJK, fontSize: 15, color: WHITE, lineSpacingMultiple: 1.15 });
  pageNo(s, 3);
})();

// =========================================================== 4 目的
(() => {
  const s = slide(WHITE);
  header(s, "目的", "一句話進，一份完整行程出");
  // left input->output flow
  card(s, 0.62, 1.95, 5.6, 4.6, PANEL);
  s.addText("你只要說一句話", { x: 0.95, y: 2.2, w: 5, h: 0.4, fontFace: CJK, fontSize: 15, bold: true, color: TEALD });
  card(s, 0.95, 2.65, 4.95, 1.0, WHITE, LINE);
  s.addText("「想去東京 5 天、預算 5 萬、愛美食與自然景觀、搭大眾運輸」",
    { x: 1.15, y: 2.7, w: 4.6, h: 0.9, valign: "middle", fontFace: CJK, fontSize: 14, italic: true, color: INK });
  s.addText("▼", { x: 0.95, y: 3.72, w: 4.95, h: 0.35, align: "center", fontFace: NUM, fontSize: 14, color: TEAL });
  s.addText("系統自動產出完整行程", { x: 0.95, y: 4.1, w: 5, h: 0.4, fontFace: CJK, fontSize: 15, bold: true, color: TEALD });
  card(s, 0.95, 4.5, 4.95, 1.8, WHITE, LINE);
  s.addText([
    { text: "每天景點 · 三餐 · 住宿\n", options: {} },
    { text: "交通時間 · 機票/飯店比價\n", options: {} },
    { text: "還附上行程地圖", options: {} },
  ], { x: 1.2, y: 4.65, w: 4.5, h: 1.55, valign: "middle", fontFace: CJK, fontSize: 15, color: INK, lineSpacingMultiple: 1.4 });

  // right: 3 principles
  s.addText("我們的三個堅持", { x: 6.65, y: 1.95, w: 6, h: 0.45, fontFace: CJK, fontSize: 17, bold: true, color: INK });
  const pr = [["不亂編", "每個景點、美食都有真實的網路文章出處"], ["真的可行", "交通時間、總花費、預算都實際算過"], ["好上手", "過程即時顯示進度，完成後一句話就能微調"]];
  pr.forEach((c, i) => {
    const y = 2.55 + i * 1.32;
    card(s, 6.65, y, 6.05, 1.15, WHITE, LINE);
    s.addShape(p.ShapeType.roundRect, { x: 6.9, y: y + 0.27, w: 0.6, h: 0.6, rectRadius: 0.08, fill: { color: TEAL } });
    s.addText(String(i + 1), { x: 6.9, y: y + 0.27, w: 0.6, h: 0.6, align: "center", valign: "middle", fontFace: NUM, fontSize: 20, bold: true, color: WHITE });
    s.addText(c[0], { x: 7.75, y: y + 0.16, w: 4.7, h: 0.42, fontFace: CJK, fontSize: 17, bold: true, color: INK });
    s.addText(c[1], { x: 7.75, y: y + 0.58, w: 4.8, h: 0.5, fontFace: CJK, fontSize: 13, color: MUTE });
  });
  pageNo(s, 4);
})();

// =========================================================== 5 方法-框架
(() => {
  const s = slide(WHITE);
  header(s, "方法 · 協作框架", "一個會分工的 AI 小團隊，接力完成");
  // manager + workers
  card(s, 0.62, 2.0, 7.2, 4.5, PANEL);
  s.addShape(p.ShapeType.roundRect, { x: 3.0, y: 2.35, w: 2.5, h: 0.95, rectRadius: 0.1, fill: { color: NAVY } });
  s.addText("總管", { x: 3.0, y: 2.4, w: 2.5, h: 0.45, align: "center", fontFace: CJK, fontSize: 17, bold: true, color: WHITE });
  s.addText("看缺什麼 · 決定下一步", { x: 3.0, y: 2.83, w: 2.5, h: 0.4, align: "center", fontFace: CJK, fontSize: 11, color: TEAL });
  const work = ["找資料", "排行程", "算交通", "比價"];
  work.forEach((t, i) => {
    const x = 0.95 + i * 1.72;
    s.addShape(p.ShapeType.line, { x: 4.25, y: 3.3, w: (x + 0.78) - 4.25, h: 0.85, line: { color: "B9C6D2", width: 1 } });
    s.addShape(p.ShapeType.roundRect, { x, y: 4.15, w: 1.55, h: 0.85, rectRadius: 0.08, fill: { color: WHITE }, line: { color: TEAL, width: 1.25 } });
    s.addText(t, { x, y: 4.15, w: 1.55, h: 0.85, align: "center", valign: "middle", fontFace: CJK, fontSize: 14, bold: true, color: TEALD });
  });
  s.addText("大家共用同一份「行程草稿」接力編修；總管動態決定順序，全程即時顯示。",
    { x: 0.95, y: 5.35, w: 6.6, h: 0.9, fontFace: CJK, fontSize: 13.5, color: INK, lineSpacingMultiple: 1.2 });

  // right: bounded retry + RL loop
  card(s, 8.05, 2.0, 4.65, 2.05, WHITE, LINE);
  s.addText("有界重做，不會卡死", { x: 8.3, y: 2.2, w: 4.2, h: 0.4, fontFace: CJK, fontSize: 15, bold: true, color: INK });
  s.addText("不滿意會自動重做，但有次數上限 —— 一定會給出結果，不會無止盡空轉。",
    { x: 8.3, y: 2.65, w: 4.15, h: 1.2, fontFace: CJK, fontSize: 13, color: MUTE, lineSpacingMultiple: 1.2 });
  card(s, 8.05, 4.25, 4.65, 2.25, NAVY);
  s.addText("與強化學習的關聯", { x: 8.3, y: 4.45, w: 4.2, h: 0.4, fontFace: CJK, fontSize: 14, bold: true, color: TEAL });
  s.addText("看狀態  →  做決定  →  看結果  →  再決定",
    { x: 8.3, y: 4.9, w: 4.2, h: 0.5, fontFace: CJK, fontSize: 13.5, bold: true, color: WHITE });
  s.addText("這個決策閉環，概念上呼應強化學習的「狀態—行動—回饋」循環。",
    { x: 8.3, y: 5.45, w: 4.2, h: 0.9, fontFace: CJK, fontSize: 12.5, color: "C7D2DF", lineSpacingMultiple: 1.2 });
  pageNo(s, 5);
})();

// =========================================================== 6 方法-Agent Skill
(() => {
  const s = slide(WHITE);
  header(s, "方法 · Agent Skill", "每位助理都有一張「技能卡」");
  // concept strip (progressive disclosure)
  card(s, 0.62, 1.72, 12.1, 1.05, NAVY);
  s.addText("漸進式揭露", { x: 0.95, y: 1.92, w: 2.4, h: 0.6, valign: "middle", fontFace: CJK, fontSize: 15, bold: true, color: TEAL });
  s.addText([
    { text: "每個助理都有自己的技能卡（SKILL.md）。", options: { bold: true, color: WHITE } },
    { text: "平時只讀「一句話描述」做分派；輪到它上場，才載入完整操作指南 —— 省 token、好維護、易擴充。", options: { color: "D2DBE6" } },
  ], { x: 3.25, y: 1.8, w: 9.25, h: 0.9, valign: "middle", fontFace: CJK, fontSize: 12.5, lineSpacingMultiple: 1.15 });

  // 7 skill rows
  const skills = [
    ["總管 Supervisor", "看進度選下一步（找資料 / 排程 / 驗證 / 比價 / 收尾），套用守則保證收斂", true],
    ["資料蒐集 Researcher", "Serper 雙查詢 + Firecrawl 擷取景點美食，做部落格 grounding", false],
    ["行程規劃 Planner", "依研究結果生成多日結構化行程，顧地理聚類與住宿連續", false],
    ["交通驗證 Route Validator", "確定性計算每日駕車 / 大眾運輸時間，標示是否違反上限", false],
    ["比價 Booker", "機票 / 飯店 / 租車比價與預算彙總，選最佳性價比（不實際下訂）", false],
    ["計劃書 Output Formatter", "輸出完整 Markdown 行程計劃書、每日交通與總預算表", false],
    ["回饋 Feedback", "解析事後一句話回饋，轉成 Planner 可讀的參數與 notes", false],
  ];
  const x = 0.62, w = 12.1, y0 = 2.98, rh = 0.555;
  skills.forEach((k, i) => {
    const y = y0 + i * rh;
    s.addShape(p.ShapeType.rect, { x, y, w, h: rh - 0.06, fill: { color: i % 2 ? WHITE : PANEL }, line: { type: "none" } });
    s.addShape(p.ShapeType.rect, { x, y, w: 0.1, h: rh - 0.06, fill: { color: k[2] ? NAVY : TEAL } });
    s.addText(k[0], { x: x + 0.32, y, w: 3.55, h: rh - 0.06, valign: "middle", fontFace: CJK, fontSize: 13.5, bold: true, color: k[2] ? NAVY : INK });
    s.addText(k[1], { x: x + 4.0, y, w: w - 4.25, h: rh - 0.06, valign: "middle", fontFace: CJK, fontSize: 12, color: MUTE, lineSpacingMultiple: 1.05 });
  });
  pageNo(s, 6);
})();

// =========================================================== 7 系統架構
(() => {
  const s = slide(WHITE);
  header(s, "系統架構 · 整體架構", "三個部分，各司其職");
  const layers = [
    ["介面", "你看到的網頁", "填需求 · 看即時進度 · 看行程 · 給回饋"],
    ["協調流程", "總管 + 各助理分工合作", "決定順序、接力產出、有界重做"],
    ["背後服務", "AI 大腦 + 查詢服務", "地圖 · 搜尋 · 比價"],
  ];
  layers.forEach((l, i) => {
    const y = 1.95 + i * 1.18;
    card(s, 0.62, y, 7.5, 1.02, i === 1 ? NAVY : PANEL);
    const dark = i === 1;
    s.addShape(p.ShapeType.rect, { x: 0.62, y, w: 0.14, h: 1.02, fill: { color: TEAL } });
    s.addText(l[0], { x: 1.0, y: y + 0.16, w: 2.3, h: 0.7, valign: "middle", fontFace: CJK, fontSize: 18, bold: true, color: dark ? WHITE : INK });
    s.addText([{ text: l[1] + "\n", options: { fontSize: 13.5, bold: true, color: dark ? TEAL : INK } }, { text: l[2], options: { fontSize: 12, color: dark ? "C7D2DF" : MUTE } }],
      { x: 3.3, y: y + 0.12, w: 4.7, h: 0.8, valign: "middle", fontFace: CJK, lineSpacingMultiple: 1.15 });
  });
  // principle panel
  card(s, 8.35, 1.95, 4.35, 3.43, TEALL);
  s.addText("核心原則", { x: 8.62, y: 2.15, w: 3.8, h: 0.4, fontFace: CJK, fontSize: 16, bold: true, color: TEALD });
  s.addText([
    { text: "需要「精準」的\n", options: { fontSize: 13, color: MUTE } },
    { text: "時間 · 金額 · 預算 → 用程式計算\n\n", options: { fontSize: 14, bold: true, color: INK } },
    { text: "需要「理解與創意」的\n", options: { fontSize: 13, color: MUTE } },
    { text: "找資料 · 排行程 → 交給 AI", options: { fontSize: 14, bold: true, color: INK } },
  ], { x: 8.62, y: 2.65, w: 3.85, h: 2.0, fontFace: CJK, lineSpacingMultiple: 1.15 });
  s.addText("AI 不亂編，結果可信、可重現。", { x: 0.62, y: 5.6, w: 12, h: 0.4, fontFace: CJK, fontSize: 13.5, bold: true, color: TEALD });
  pageNo(s, 7);
})();

// =========================================================== 8 團隊分工
(() => {
  const s = slide(WHITE);
  header(s, "系統架構 · 團隊分工", "六位助理，一條龍完成");
  const roles = [
    ["總管", "看進度，決定下一步、何時收尾"],
    ["資料蒐集員", "上網找景點與美食，整理成有出處的清單"],
    ["行程規劃師", "把資料排成一天天的行程"],
    ["交通驗證員", "算每天交通時間、畫出路線地圖"],
    ["比價員", "查機票、飯店，估花費、對照預算"],
    ["回饋助理", "把你的一句話意見，轉成具體調整"],
  ];
  const gx = 0.62, gy = 1.95, cw = 3.93, ch = 2.05, gxp = 0.18, gyp = 0.25;
  roles.forEach((r, i) => {
    const x = gx + (i % 3) * (cw + gxp), y = gy + Math.floor(i / 3) * (ch + gyp);
    card(s, x, y, cw, ch, WHITE, LINE);
    s.addShape(p.ShapeType.rect, { x: x, y: y, w: cw, h: 0.12, fill: { color: i === 0 ? NAVY : TEAL } });
    s.addText(r[0], { x: x + 0.3, y: y + 0.42, w: cw - 0.6, h: 0.5, fontFace: CJK, fontSize: 18, bold: true, color: i === 0 ? NAVY : INK });
    s.addText(r[1], { x: x + 0.3, y: y + 1.0, w: cw - 0.55, h: 0.9, fontFace: CJK, fontSize: 13, color: MUTE, lineSpacingMultiple: 1.2 });
  });
  pageNo(s, 8);
})();

// =========================================================== 9 流程
(() => {
  const s = slide(WHITE);
  header(s, "系統架構 · 流程", "像一場接力，全程看得見");
  const steps = ["聽懂需求", "找資料", "排行程", "驗證交通", "比價", "計劃書"];
  const n = steps.length, x0 = 0.62, totalW = 12.1, bw = 1.72, gap = (totalW - bw * n) / (n - 1), y = 2.15;
  steps.forEach((t, i) => {
    const x = x0 + i * (bw + gap);
    const last = i === n - 1;
    s.addShape(p.ShapeType.roundRect, { x, y, w: bw, h: 1.1, rectRadius: 0.1, fill: { color: last ? NAVY : (i === 0 ? TEAL : WHITE) }, line: (last || i === 0) ? { type: "none" } : { color: TEAL, width: 1.25 } });
    s.addText(t, { x, y, w: bw, h: 1.1, align: "center", valign: "middle", fontFace: CJK, fontSize: 14.5, bold: true, color: (last || i === 0) ? WHITE : TEALD });
    if (i < n - 1) s.addText("›", { x: x + bw - 0.02, y: y, w: gap + 0.04, h: 1.1, align: "center", valign: "middle", fontFace: NUM, fontSize: 22, bold: true, color: "9AA8B5" });
  });
  const notes = [
    ["即時顯示", "每一步都看得到：找到哪些文章、排了哪些景點、比價結果、總管為什麼這樣決定。"],
    ["保證收斂", "就算一時不完美，也會在合理次數內收尾、誠實標示，不會無限卡住。"],
    ["一句話微調", "完成後說「第二天移動太久」，系統只重排需要的部分，又快又省。"],
  ];
  notes.forEach((c, i) => {
    const x = 0.62 + i * 4.06;
    card(s, x, 3.9, 3.8, 2.3, PANEL);
    s.addShape(p.ShapeType.ellipse, { x: x + 0.3, y: 4.18, w: 0.46, h: 0.46, fill: { color: TEAL } });
    s.addText(String(i + 1), { x: x + 0.3, y: 4.18, w: 0.46, h: 0.46, align: "center", valign: "middle", fontFace: NUM, fontSize: 14, bold: true, color: WHITE });
    s.addText(c[0], { x: x + 0.9, y: 4.18, w: 2.7, h: 0.46, valign: "middle", fontFace: CJK, fontSize: 16, bold: true, color: INK });
    s.addText(c[1], { x: x + 0.32, y: 4.82, w: 3.3, h: 1.25, fontFace: CJK, fontSize: 13, color: MUTE, lineSpacingMultiple: 1.25 });
  });
  pageNo(s, 9);
})();

// =========================================================== 10 LangGraph
(() => {
  const s = slide(WHITE);
  header(s, "系統架構 · LangGraph", "用狀態機，把多代理串成自主流程");

  // ---- left: hub-and-spoke graph ----
  const supX = 1.15, supY = 3.55, supW = 2.25, supH = 1.05;
  const supCx = supX + supW, supCy = supY + supH / 2;
  const wkX = 4.55, wkW = 2.0, wkH = 0.62;
  const workers = [["research", "找資料", 2.0], ["generate_draft", "排行程", 2.92], ["validate_route", "驗證交通", 3.84], ["booker", "比價", 4.76]];
  // spokes (supervisor ⇄ workers)
  workers.forEach((w) => {
    const cy = w[2] + wkH / 2;
    s.addShape(p.ShapeType.line, { x: supCx, y: supCy, w: wkX - supCx, h: cy - supCy, line: { color: "B9C6D2", width: 1.25, endArrowType: "triangle", beginArrowType: "triangle" } });
  });
  // parse (entry) -> supervisor
  s.addShape(p.ShapeType.roundRect, { x: supX, y: 2.05, w: supW, h: 0.62, rectRadius: 0.08, fill: { color: WHITE }, line: { color: TEAL, width: 1.25 } });
  s.addText("parse 解析需求", { x: supX, y: 2.05, w: supW, h: 0.62, align: "center", valign: "middle", fontFace: CJK, fontSize: 12.5, bold: true, color: TEALD });
  s.addShape(p.ShapeType.line, { x: supX + supW / 2, y: 2.67, w: 0, h: supY - 2.67, line: { color: "9AA8B5", width: 1.25, endArrowType: "triangle" } });
  // supervisor hub
  s.addShape(p.ShapeType.roundRect, { x: supX, y: supY, w: supW, h: supH, rectRadius: 0.1, fill: { color: NAVY } });
  s.addText([{ text: "Supervisor\n", options: { fontSize: 15, bold: true, color: WHITE } }, { text: "總管 · 中樞派工", options: { fontSize: 11, color: TEAL } }], { x: supX, y: supY, w: supW, h: supH, align: "center", valign: "middle", fontFace: CJK, lineSpacingMultiple: 1.1 });
  // worker chips
  workers.forEach((w) => {
    s.addShape(p.ShapeType.roundRect, { x: wkX, y: w[2], w: wkW, h: wkH, rectRadius: 0.08, fill: { color: PANEL }, line: { color: TEAL, width: 1 } });
    s.addText(w[1], { x: wkX, y: w[2], w: wkW, h: wkH, align: "center", valign: "middle", fontFace: CJK, fontSize: 12.5, bold: true, color: INK });
  });
  s.addText("派工 → 回報，有界重試", { x: wkX - 0.1, y: 5.5, w: wkW + 0.4, h: 0.35, align: "center", fontFace: CJK, fontSize: 10.5, italic: true, color: MUTE });
  // supervisor -> format_output -> END
  s.addShape(p.ShapeType.line, { x: supX + supW / 2, y: supY + supH, w: 0, h: 5.55 - (supY + supH), line: { color: "9AA8B5", width: 1.25, endArrowType: "triangle" } });
  s.addShape(p.ShapeType.roundRect, { x: supX, y: 5.55, w: supW, h: 0.62, rectRadius: 0.08, fill: { color: TEAL } });
  s.addText("format_output", { x: supX, y: 5.55, w: supW, h: 0.62, align: "center", valign: "middle", fontFace: NUM, fontSize: 12.5, bold: true, color: WHITE });
  s.addText("→ END", { x: supX + supW + 0.15, y: 5.55, w: 1.2, h: 0.62, valign: "middle", fontFace: NUM, fontSize: 13, bold: true, color: MUTE });

  // ---- right: concept cards ----
  const cards = [
    ["共享狀態 StateGraph", "所有代理讀寫同一份行程狀態，接力編修、無縫傳遞。"],
    ["節點 + 條件邊", "節點即各代理；Supervisor 用 conditional edges 依狀態動態派工（hub-and-spoke）。"],
    ["有界迴圈", "可重試但設重試／步數上限，保證一定收斂、不空轉卡死。"],
    ["逐步串流", "以 .stream() 每完成一個節點即時回傳，過程全程看得見。"],
  ];
  const cx = 7.75, cw = 4.95, cy0 = 2.0, chh = 1.05, cvg = 0.12;
  cards.forEach((c, i) => {
    const y = cy0 + i * (chh + cvg);
    card(s, cx, y, cw, chh, i === 1 ? TEALL : WHITE, i === 1 ? null : LINE);
    s.addShape(p.ShapeType.rect, { x: cx, y, w: 0.1, h: chh, fill: { color: TEAL } });
    s.addText(c[0], { x: cx + 0.3, y: y + 0.14, w: cw - 0.5, h: 0.4, fontFace: CJK, fontSize: 14, bold: true, color: INK });
    s.addText(c[1], { x: cx + 0.3, y: y + 0.52, w: cw - 0.55, h: 0.5, fontFace: CJK, fontSize: 11.5, color: MUTE, lineSpacingMultiple: 1.15 });
  });
  pageNo(s, 10);
})();

// =========================================================== 11 問題 1
(() => {
  const s = slide(WHITE);
  header(s, "遇到的問題與解決 · 1 / 2", "內容品質：從空泛、錯價到具體可信");
  const rows = [
    ["行程「空泛」", "同一個地標天天出現、餐廳寫得很籠統", "讀更完整、更多篇文章；每天安排不同景點，不足時補上精選清單", "每天景點美食都不同、具體，且有真實出處"],
    ["價格「離譜」", "門票出現「兩萬多元」這種金額", "日文價格是日圓，加入自動幣別換算，一律換成台幣再顯示", "所有金額回到合理範圍"],
  ];
  rows.forEach((r, i) => {
    const y = 1.95 + i * 2.35;
    s.addText(r[0], { x: 0.62, y: y + 0.3, w: 2.2, h: 1.6, valign: "middle", fontFace: CJK, fontSize: 18, bold: true, color: INK });
    // before
    card(s, 2.95, y, 4.5, 2.1, REDL);
    s.addText("之前", { x: 3.2, y: y + 0.18, w: 2, h: 0.35, fontFace: CJK, fontSize: 12, bold: true, color: RED, charSpacing: 1 });
    s.addText(r[1], { x: 3.2, y: y + 0.6, w: 4.0, h: 1.3, valign: "top", fontFace: CJK, fontSize: 14.5, color: INK, lineSpacingMultiple: 1.2 });
    s.addText("›", { x: 7.45, y: y, w: 0.7, h: 2.1, align: "center", valign: "middle", fontFace: NUM, fontSize: 26, bold: true, color: TEAL });
    // after
    card(s, 8.2, y, 4.5, 2.1, GRNL);
    s.addText("之後", { x: 8.45, y: y + 0.18, w: 2, h: 0.35, fontFace: CJK, fontSize: 12, bold: true, color: GRN, charSpacing: 1 });
    s.addText(r[3], { x: 8.45, y: y + 0.6, w: 4.0, h: 0.7, fontFace: CJK, fontSize: 15, bold: true, color: INK });
    s.addText("作法：" + r[2], { x: 8.45, y: y + 1.28, w: 4.05, h: 0.7, fontFace: CJK, fontSize: 11.5, color: MUTE, lineSpacingMultiple: 1.15 });
  });
  pageNo(s, 11);
})();

// =========================================================== 12 問題 2
(() => {
  const s = slide(WHITE);
  header(s, "遇到的問題與解決 · 2 / 2", "流程順暢與正確：不卡死、選什麼就照什麼");
  const items = [
    ["系統「太嚴格」反而卡住", "稍微超出預算就一直重排，最後仍給不出結果。", "把「超出預算」「不夠完美」改成善意提醒、不再硬性擋下；該收尾就收尾。", "順利產出完整行程；超預算就清楚標示金額與建議。"],
    ["大眾運輸顯示成「開車時間」", "明明選了大眾運輸，交通時間卻照開車算。", "正確區分交通方式，分開估算並標示大眾運輸的移動時間。", "你選什麼交通方式，行程就照那個方式呈現。"],
  ];
  items.forEach((r, i) => {
    const y = 1.95 + i * 2.35;
    card(s, 0.62, y, 12.1, 2.1, WHITE, LINE);
    s.addShape(p.ShapeType.rect, { x: 0.62, y, w: 0.14, h: 2.1, fill: { color: AMBER } });
    s.addText(r[0], { x: 1.0, y: y + 0.22, w: 11.4, h: 0.5, fontFace: CJK, fontSize: 18, bold: true, color: INK });
    const cols = [["問題", r[1], MUTE], ["解法", r[2], TEALD], ["成果", r[3], GRN]];
    cols.forEach((c, j) => {
      const x = 1.0 + j * 3.92;
      s.addText(c[0], { x, y: y + 0.82, w: 3.7, h: 0.34, fontFace: CJK, fontSize: 12, bold: true, color: c[2], charSpacing: 1 });
      s.addText(c[1], { x, y: y + 1.16, w: 3.72, h: 0.85, fontFace: CJK, fontSize: 12.5, color: INK, lineSpacingMultiple: 1.18 });
    });
  });
  pageNo(s, 12);
})();

// =========================================================== 13 限制
(() => {
  const s = slide(WHITE);
  header(s, "限制", "誠實面對目前的不足");
  const lim = [
    ["資料豐富度不一", "不同城市、文章的資料量不同；有些景點沒寫明票價，金額估算會偏保守。"],
    ["生成需要時間", "產出一份完整行程約需數分鐘（要查很多資料、跑很多步驟）。"],
    ["尚不能存檔續跑", "目前無法中途存檔、之後再接續執行。"],
    ["複雜行程偶有不穩", "面對非常複雜的整份行程時，AI 偶爾不夠穩定，仍在持續優化。"],
  ];
  lim.forEach((c, i) => {
    const y = 2.0 + i * 1.12;
    card(s, 0.62, y, 12.1, 0.95, i % 2 ? WHITE : PANEL, i % 2 ? LINE : null);
    s.addShape(p.ShapeType.roundRect, { x: 0.92, y: y + 0.22, w: 0.5, h: 0.5, rectRadius: 0.25, fill: { color: AMBER } });
    s.addText("!", { x: 0.92, y: y + 0.22, w: 0.5, h: 0.5, align: "center", valign: "middle", fontFace: NUM, fontSize: 18, bold: true, color: WHITE });
    s.addText(c[0], { x: 1.65, y: y + 0.16, w: 3.3, h: 0.65, valign: "middle", fontFace: CJK, fontSize: 16, bold: true, color: INK });
    s.addText(c[1], { x: 5.1, y: y + 0.16, w: 7.4, h: 0.65, valign: "middle", fontFace: CJK, fontSize: 13, color: MUTE, lineSpacingMultiple: 1.15 });
  });
  pageNo(s, 13);
})();

// =========================================================== 14 DEMO
(() => {
  const s = slide(WHITE);
  header(s, "Demo", "實際畫面：一句話進，計劃書出");
  // filmstrip of 4 real screenshots
  const shots = [
    ["assets/ui_research.png", 1.893, "找資料", "解析需求、搜尋部落格擷取景點與美食（有出處）"],
    ["assets/ui_agents.png", 1.475, "排行程・驗證・比價", "即時串流：排行程、算交通時間、比價並檢查預算"],
    ["assets/ui_itinerary.png", 1.722, "每日行程", "一天天的景點、三餐、住宿與駕車時間"],
    ["assets/ui_plan_map.png", 1.808, "旅遊計劃書", "完整計劃書，附每日路線行程地圖"],
  ];
  const x0 = 0.62, totalW = 12.08, n = 4, arrowW = 0.34;
  const cw = (totalW - (n - 1) * arrowW) / n; // ~2.85
  const frameY = 2.45, frameH = 2.05;
  shots.forEach((sh, i) => {
    const x = x0 + i * (cw + arrowW);
    s.addText(sh[2], { x: x - 0.05, y: 1.78, w: cw + 0.1, h: 0.36, align: "center", valign: "middle", fontFace: CJK, fontSize: 13, bold: true, color: TEALD });
    s.addText(String(i + 1).padStart(2, "0"), { x: x - 0.05, y: 2.12, w: cw + 0.1, h: 0.26, align: "center", fontFace: NUM, fontSize: 10, bold: true, color: MUTE, charSpacing: 1 });
    // frame card sized to image aspect, centered in column
    let iw = cw, ih = iw / sh[1];
    if (ih > frameH) { ih = frameH; iw = ih * sh[1]; }
    const ix = x + (cw - iw) / 2, iy = frameY + (frameH - ih) / 2;
    s.addShape(p.ShapeType.roundRect, { x: ix - 0.04, y: iy - 0.04, w: iw + 0.08, h: ih + 0.08, rectRadius: 0.05, fill: { color: NAVY }, line: { color: TEAL, width: 1 }, shadow: { type: "outer", color: "9AA8B5", opacity: 0.3, blur: 6, offset: 2, angle: 90 } });
    s.addImage({ path: sh[0], x: ix, y: iy, w: iw, h: ih });
    s.addText(sh[3], { x: x - 0.05, y: 4.62, w: cw + 0.1, h: 0.78, align: "center", valign: "top", fontFace: CJK, fontSize: 10.5, color: MUTE, lineSpacingMultiple: 1.12 });
    if (i < n - 1) s.addText("›", { x: x + cw, y: frameY, w: arrowW, h: frameH, align: "center", valign: "middle", fontFace: NUM, fontSize: 22, bold: true, color: "9AA8B5" });
  });
  // bottom strip
  card(s, 0.62, 5.55, 12.1, 1.0, NAVY);
  s.addText("Demo 影片", { x: 0.95, y: 5.78, w: 2.2, h: 0.55, valign: "middle", fontFace: CJK, fontSize: 15, bold: true, color: TEAL });
  s.addText("輸入「東京 5 天、預算 5 萬、愛美食與自然、搭大眾運輸」 → 全程即時看它完成 → 再用一句話微調「第二天移動太久」，系統局部重排。",
    { x: 3.1, y: 5.62, w: 9.4, h: 0.9, valign: "middle", fontFace: CJK, fontSize: 12.5, color: "D2DBE6", lineSpacingMultiple: 1.15 });
  pageNo(s, 14);
})();

// =========================================================== 15 總結
(() => {
  const s = slide(NAVY);
  s.addShape(p.ShapeType.rect, { x: 0.62, y: 0.62, w: 0.16, h: 0.16, fill: { color: TEAL } });
  s.addText("總結", { x: 0.86, y: 0.54, w: 9, h: 0.32, fontFace: CJK, fontSize: 13, bold: true, color: TEAL, charSpacing: 2 });
  s.addText("一句話，換一份算過、有出處、附地圖的完整行程", { x: 0.6, y: 1.15, w: 12.1, h: 1.2, fontFace: CJK, fontSize: 30, bold: true, color: WHITE, lineSpacingMultiple: 1.1 });

  const pts = [
    ["分工協作", "會分工的 AI 旅遊助理：總管派工、各助理接力完成。"],
    ["聰明又可靠", "需要精準的交給程式、需要理解的交給 AI，兩者分工。"],
    ["像真產品", "過程即時可見、結果可一句話微調。"],
  ];
  pts.forEach((c, i) => {
    const x = 0.62 + i * 4.06;
    s.addShape(p.ShapeType.roundRect, { x, y: 2.85, w: 3.8, h: 1.95, rectRadius: 0.1, fill: { color: NAVY2 }, line: { color: "32425E", width: 1 } });
    s.addText(c[0], { x: x + 0.3, y: 3.1, w: 3.2, h: 0.5, fontFace: CJK, fontSize: 17, bold: true, color: TEAL });
    s.addText(c[1], { x: x + 0.3, y: 3.65, w: 3.25, h: 1.0, fontFace: CJK, fontSize: 13, color: "D2DBE6", lineSpacingMultiple: 1.25 });
  });

  s.addText("未來方向", { x: 0.62, y: 5.2, w: 3, h: 0.4, fontFace: CJK, fontSize: 14, bold: true, color: TEAL });
  s.addText("行程安排更細緻　·　價格更精準　·　加入與其他系統的比較評測",
    { x: 0.62, y: 5.6, w: 12, h: 0.45, fontFace: CJK, fontSize: 14, color: "C7D2DF" });
  s.addShape(p.ShapeType.line, { x: 0.62, y: 6.25, w: 12.1, h: 0, line: { color: "32425E", width: 1 } });
  s.addText("Demo　/　Q & A", { x: 0.62, y: 6.45, w: 12.1, h: 0.6, align: "center", fontFace: CJK, fontSize: 20, bold: true, color: WHITE });
})();

p.writeFile({ fileName: "自主式旅遊行程簡報_v4.pptx" }).then(f => console.log("WROTE", f));
