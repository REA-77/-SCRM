import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "D:/下载/运营部_所有账号.xlsx";
const outputDir = "D:/下载/数据库/outputs/homepage-link-sync";
const outputPath = `${outputDir}/运营部_所有账号_已同步主页链接.xlsx`;

const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("所有账号");

// Append the field beside the existing account table so no existing columns move.
sheet.getRange("CS1:CS225").copyFrom(sheet.getRange("B1:B225"), "all");
sheet.getRange("CS1").values = [["主页链接"]];
sheet.getRange("CS2").formulasR1C1 = [[
  '=IFERROR(IF(LEFT(RC2,FIND(",",RC2&",")-1)="B站",RC11,IF(LEFT(RC2,FIND(",",RC2&",")-1)="抖音",RC23,IF(LEFT(RC2,FIND(",",RC2&",")-1)="知乎",RC26,IF(LEFT(RC2,FIND(",",RC2&",")-1)="视频号",RC47,IF(LEFT(RC2,FIND(",",RC2&",")-1)="小红书",RC52,IF(LEFT(RC2,FIND(",",RC2&",")-1)="微博",RC61,"")))))),"")'
]];
sheet.getRange("CS2:CS225").fillDown();
sheet.getRange("CS1:CS225").format.wrapText = true;
sheet.getRange("CS1:CS225").format.columnWidth = 36;

workbook.recalculate();

const check = await workbook.inspect({
  kind: "table",
  range: "CS1:CS8",
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 4,
});
console.log(check.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "所有账号",
  range: "CS1:CS16",
  scale: 2,
  format: "png",
});
await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(`${outputDir}/主页链接预览.png`, new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath }));
