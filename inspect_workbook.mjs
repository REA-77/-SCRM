import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "D:/下载/运营部_所有账号.xlsx";
const outputDir = "D:/下载/数据库/.workbook-inspection";
await fs.mkdir(outputDir, { recursive: true });
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const summary = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 30000,
  tableMaxRows: 20,
  tableMaxCols: 30,
  tableMaxCellChars: 120,
});
console.log(summary.ndjson);
for (const sheet of workbook.worksheets.items) {
  const preview = await workbook.render({ sheetName: sheet.name, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(`${outputDir}/${sheet.name.replace(/[\\/:*?\"<>|]/g, "_")}.png`, new Uint8Array(await preview.arrayBuffer()));
}
