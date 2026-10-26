import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "D:/下载/数据库/outputs/homepage-link-sync/运营部_所有账号_已同步主页链接.xlsx";
const outputDir = "D:/下载/数据库/outputs/platform-operation-account-list";
const outputPath = `${outputDir}/平台运营账户列表_含主页链接.xlsx`;

const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const source = workbook.worksheets.getItem("所有账号");
const sheet = workbook.worksheets.add("平台运营账户列表");

sheet.getRange("A1:E1").values = [["账号名称", "平台", "主页链接", "运营", "运营(新)"]];
sheet.getRange("A2:E2").formulas = [[
  "='所有账号'!$A2",
  "='所有账号'!$B2",
  "='所有账号'!$CS2",
  "=IF('所有账号'!$C2=\"\",\"\",'所有账号'!$C2)",
  "=IF('所有账号'!$D2=\"\",\"\",'所有账号'!$D2)",
]];
sheet.getRange("A2:E225").fillDown();

sheet.getRange("A1:E1").format = {
  fill: "#1F4E78",
  font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
sheet.getRange("A2:E225").format = {
  font: { name: "Arial", size: 10, color: "#000000" },
  verticalAlignment: "center",
};
sheet.getRange("C2:C225").format.wrapText = true;
sheet.getRange("A1:A225").format.columnWidth = 20;
sheet.getRange("B1:B225").format.columnWidth = 28;
sheet.getRange("C1:C225").format.columnWidth = 48;
sheet.getRange("D1:E225").format.columnWidth = 14;
sheet.getRange("A1:E225").format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };
sheet.getRange("A1:E225").format.autofitRows();
sheet.freezePanes.freezeRows(1);
sheet.tables.add("A1:E225", true, "PlatformOperationAccountList");

workbook.recalculate();
const check = await workbook.inspect({
  kind: "table",
  range: "A1:E10",
  include: "values,formulas",
  tableMaxRows: 10,
  tableMaxCols: 5,
});
console.log(check.ndjson);
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);
const preview = await workbook.render({ sheetName: "平台运营账户列表", range: "A1:E16", scale: 2, format: "png" });
await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(`${outputDir}/平台运营账户列表预览.png`, new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, sourceRange: source.getUsedRange().address }));
