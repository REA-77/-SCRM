import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "D:/下载/运营部_所有账号.xlsx";
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);

const summary = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 12000,
  tableMaxRows: 12,
  tableMaxCols: 20,
  tableMaxCellChars: 80,
});
console.log(summary.ndjson);

const sheet = workbook.worksheets.getItem("所有账号");
const headers = sheet.getRange("A1:CR1").values[0];
headers.forEach((header, index) => console.log(JSON.stringify({ column: index + 1, header })));

const rows = sheet.getRange("A2:CR225").values;
const nameIndex = headers.indexOf("账号名称");
const bitNameIndex = headers.indexOf("比特窗口");
const bitNumberIndex = headers.indexOf("BIT窗口号");
const bitRows = rows
  .filter(row => row[bitNameIndex] || row[bitNumberIndex])
  .map((row, index) => ({ row: index + 2, name: row[nameIndex], bitWindow: row[bitNameIndex], bitWindowNumber: row[bitNumberIndex] }));
console.log(JSON.stringify({ bitRowsCount: bitRows.length, bitRows }, null, 2));
