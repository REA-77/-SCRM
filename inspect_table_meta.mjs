import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/运营部_所有账号.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("所有账号");
const table = sheet.tables.items[0];
const formulas = await workbook.inspect({ kind: "formula", sheetId: sheet.sheetId, range: "A1:CR225", maxChars: 30000, options: { maxResults: 300 } });
console.log(JSON.stringify({ tableName: table.name, style: table.style, showHeaders: table.showHeaders, showTotals: table.showTotals, showBandedColumns: table.showBandedColumns, showFilterButton: table.showFilterButton }, null, 2));
console.log(formulas.ndjson);
