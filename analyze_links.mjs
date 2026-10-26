import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/运营部_所有账号.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("所有账号");
const data = sheet.getRange("A1:CR225").values;
const headers = data[0];
const ix = Object.fromEntries(headers.map((h, i) => [h, i]));
const linkFields = ["B站主页链接", "抖音主页", "知乎主页", "视频号主页链接", "小红书主页链接", "微博主页链接"];
const results = {};
for (const row of data.slice(1)) {
  const platform = row[ix["平台"]] ?? "(空)";
  if (!results[platform]) results[platform] = { rows: 0, populated: {}, samples: [] };
  const entry = results[platform];
  entry.rows++;
  const rowLinks = {};
  for (const field of linkFields) {
    const value = row[ix[field]];
    if (value != null && value !== "") {
      entry.populated[field] = (entry.populated[field] ?? 0) + 1;
      rowLinks[field] = value;
    }
  }
  if (Object.keys(rowLinks).length && entry.samples.length < 4) entry.samples.push({ account: row[ix["账号名称"]], ...rowLinks });
}
console.log(JSON.stringify({ results, linkFields }, null, 2));
