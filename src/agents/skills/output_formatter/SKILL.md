---
name: output_formatter
description: 產生完整 Markdown 行程計劃書：每日交通、景點表、餐飲 TWD、住宿、當日小計與總預算表。
---

# Output Formatter Skill

## 目標

由 `build_itinerary_md()` 產出使用者可讀的 `final_itinerary_md`。此節點**不使用 LLM**。

## 文件結構

```
# {destination} {days} 天{交通方式}行程計劃書

## Day N（日期）
> 當日 notes

### 交通
- 自駕總計 / 大眾運輸總計
- 各段 from → to（模式, 分鐘, km）

### 景點
| 景點 | 地點 | 停留 | 門票(TWD) | 備註 |

### 餐飲
| 時段 | 餐廳 | 地點 | 料理 | 預估(TWD) |
（僅 breakfast / lunch / dinner）

### 住宿
- 名稱、地點、每晚 TWD

### 當日預估花費
- 門票 + 餐飲 + 住宿小計

## 總預算與比價
- 機票/住宿/租車/大眾/餐飲/門票/grand_total
- 機票比價表、飯店比價表（若有 price_summary）

## 注意事項 / 仍待改善
- hard violations 列表
- price_summary 為空時標註「尚未完成比價」
```

## 花費誠實標記

- `price_summary` 為空 → 文首註記粗估；`format_output` 的 `status` 不應為 `success`
- 有比價 → 使用 `price_summary` 各欄位與 `within_budget`

## 餐飲呈現

只輸出 `dp.meals` 中 `breakfast|lunch|dinner`；忽略污染 key（如 `activities`、`hotel`）。

## 參考實作

- `src/agents/output_formatter.py` — `build_itinerary_md`
- `src/core/graph.py` — `format_output`