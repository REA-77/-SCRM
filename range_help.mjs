import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/数据库/outputs/homepage-link-sync/运营部_所有账号_已同步主页链接.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
console.log(workbook.help("range.insert", { include: "index,examples,notes", maxChars: 5000 }).ndjson);
