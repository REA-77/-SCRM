import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/运营部_所有账号.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("所有账号");
console.log(JSON.stringify({ headers: sheet.getRange("A1:CR1").values[0] }, null, 2));
console.log(JSON.stringify((await workbook.inspect({
  kind: "table,region",
  sheetId: sheet.sheetId,
  range: "A1:CR225",
  maxChars: 50000,
  tableMaxRows: 8,
  tableMaxCols: 96,
  tableMaxCellChars: 100,
})).ndjson));
