import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/数据库/outputs/platform-operation-account-list/平台运营账户列表_含主页链接.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("平台运营账户列表");
console.log(JSON.stringify({
  usedRange: sheet.getUsedRange().address,
  headers: sheet.getRange("A1:E1").values,
  values: sheet.getRange("A2:E6").values,
  homePageFormulas: sheet.getRange("C2:C6").formulas,
}, null, 2));
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "saved workbook formula error scan",
});
console.log(errors.ndjson);
