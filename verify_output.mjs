import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/数据库/outputs/homepage-link-sync/运营部_所有账号_已同步主页链接.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("所有账号");
console.log(JSON.stringify({
  usedRange: sheet.getUsedRange().address,
  header: sheet.getRange("CS1").values,
  sampleValues: sheet.getRange("CS2:CS8").values,
  sampleFormulas: sheet.getRange("CS2:CS8").formulas,
}, null, 2));
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "saved workbook formula error scan",
});
console.log(errors.ndjson);
