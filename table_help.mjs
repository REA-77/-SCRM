import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/运营部_所有账号.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
console.log(workbook.help("table.resize", { include: "index,examples,notes", maxChars: 5000 }).ndjson);
