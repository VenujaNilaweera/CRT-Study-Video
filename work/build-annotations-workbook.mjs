import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const outputDir = 'C:/Users/MSI/Documents/Codex/2026-08-16/referenced-chatgpt-conversation-this-is-an/outputs';
const outputPath = path.join(outputDir, 'crt-study-annotations.xlsx');
const previewPath = path.join(outputDir, 'crt-study-annotations-summary.png');

await fs.mkdir(outputDir, { recursive: true });

const workbook = Workbook.create();

const instructions = workbook.worksheets.add('Instructions');
instructions.showGridLines = false;
instructions.getRange('A1:F1').merge();
instructions.getRange('A1').values = [['CRT Study Annotation Workbook']];
instructions.getRange('A1').format = {
  fill: '#0E2A2F',
  font: { bold: true, color: '#FFFFFF', size: 16 },
  alignment: { horizontal: 'center', vertical: 'center' },
};
instructions.getRange('A2:F2').merge();
instructions.getRange('A2').values = [[
  'Use this file to review the exported study log. The web app stores annotations locally and can export a fresh CSV log after each session.'
]];
instructions.getRange('A2').format = {
  font: { color: '#16383E' },
  alignment: { wrapText: true, vertical: 'center' },
};
instructions.getRange('A4').values = [['What this file contains']];
instructions.getRange('A4').format = { font: { bold: true, color: '#0E2A2F' } };
instructions.getRange('A5:B8').values = [
  ['Annotations', 'One row per marked video, including annotator and time'],
  ['Summary', 'Auto-calculated counts and average timing'],
  ['Refresh flow', 'Export the web app log and paste/import it here if you want a workbook copy'],
  ['Current state', 'Empty template ready for local use'],
];
instructions.getRange('A5:B8').format = {
  borders: { preset: 'outside', style: 'thin', color: '#D7E0DE' },
};
instructions.getRange('A5:A8').format = { font: { bold: true, color: '#16383E' } };
instructions.getRange('A5:B8').format.alignment = { vertical: 'center', wrapText: true };
instructions.getRange('A1:F2').format.rowHeight = 28;
instructions.getRange('A2:F2').format.rowHeight = 44;
instructions.getRange('A5:B8').format.columnWidthPx = 260;
instructions.getRange('A5:A8').format.columnWidthPx = 180;
instructions.getRange('C1:F20').format.columnWidthPx = 20;

const log = workbook.worksheets.add('Annotations');
log.showGridLines = false;
log.freezePanes.freezeRows(1);
log.getRange('A1:H1').values = [[
  'Name',
  'Role',
  'Age Group',
  'Collection',
  'Video #',
  'Video Title',
  'Time (s)',
  'Submitted At',
]];
log.getRange('A1:H1').format = {
  fill: '#0E2A2F',
  font: { bold: true, color: '#FFFFFF' },
  alignment: { horizontal: 'center', vertical: 'center', wrapText: true },
  borders: { preset: 'outside', style: 'thin', color: '#0E2A2F' },
};
log.getRange('A2:H2').values = [[
  '',
  '',
  '',
  '',
  '',
  '',
  '',
  '',
]];
log.getRange('A2:H2').format = {
  borders: { preset: 'outside', style: 'thin', color: '#D7E0DE' },
};
log.getRange('A:H').format = { alignment: { vertical: 'center' } };
log.getRange('A:A').format.columnWidthPx = 170;
log.getRange('B:B').format.columnWidthPx = 130;
log.getRange('C:C').format.columnWidthPx = 110;
log.getRange('D:D').format.columnWidthPx = 150;
log.getRange('E:E').format.columnWidthPx = 85;
log.getRange('F:F').format.columnWidthPx = 150;
log.getRange('G:G').format.columnWidthPx = 95;
log.getRange('H:H').format.columnWidthPx = 170;
log.getRange('G:G').setNumberFormat('0.00');
log.getRange('H:H').setNumberFormat('yyyy-mm-dd hh:mm:ss');

const summary = workbook.worksheets.add('Summary');
summary.showGridLines = false;
summary.getRange('A1:D1').merge();
summary.getRange('A1').values = [['CRT Study Summary']];
summary.getRange('A1').format = {
  fill: '#1E7A6F',
  font: { bold: true, color: '#FFFFFF', size: 15 },
  alignment: { horizontal: 'center', vertical: 'center' },
};
summary.getRange('A3:B6').values = [
  ['Total annotations', null],
  ['Average time (s)', null],
  ['Latest submission', null],
  ['Unique annotators', null],
];
summary.getRange('B3').formulas = [[`=COUNTA('Annotations'!A2:A1000)`]];
summary.getRange('B4').formulas = [[`=IFERROR(AVERAGE('Annotations'!G2:G1000),"")`]];
summary.getRange('B5').formulas = [[`=IFERROR(MAX('Annotations'!H2:H1000),"")`]];
summary.getRange('B6').formulas = [[`=IFERROR(SUMPRODUCT(1/COUNTIF('Annotations'!A2:A1000,'Annotations'!A2:A1000&"")),"")`]];
summary.getRange('A3:B6').format = {
  borders: { preset: 'outside', style: 'thin', color: '#D7E0DE' },
};
summary.getRange('A3:A6').format = { font: { bold: true, color: '#16383E' } };
summary.getRange('B4').setNumberFormat('0.00');
summary.getRange('B5').setNumberFormat('yyyy-mm-dd hh:mm:ss');
summary.getRange('A1:D1').format.rowHeight = 28;
summary.getRange('A3:B6').format.rowHeight = 24;
summary.getRange('A:A').format.columnWidthPx = 180;
summary.getRange('B:B').format.columnWidthPx = 180;
summary.getRange('C:D').format.columnWidthPx = 20;

const notes = workbook.worksheets.add('Notes');
notes.showGridLines = false;
notes.getRange('A1:F1').merge();
notes.getRange('A1').values = [['How the web app should behave']];
notes.getRange('A1').format = {
  fill: '#0E2A2F',
  font: { bold: true, color: '#FFFFFF', size: 15 },
  alignment: { horizontal: 'center', vertical: 'center' },
};
notes.getRange('A3:B8').values = [
  ['1', 'Each annotator logs in with name and role.'],
  ['2', 'For each video, they press Mark color change at the moment they see the skin color return.'],
  ['3', 'The app saves the row locally and advances to the next video.'],
  ['4', 'Use Export log to download a CSV copy that opens in Excel.'],
  ['5', 'If you want a true auto-updating workbook, the browser needs a file-handle save flow or a local helper.'],
  ['6', 'This workbook is a clean starting template for that flow.'],
];
notes.getRange('A3:B8').format = {
  borders: { preset: 'outside', style: 'thin', color: '#D7E0DE' },
};
notes.getRange('A3:A8').format = { font: { bold: true, color: '#1E7A6F' } };
notes.getRange('B3:B8').format = { alignment: { wrapText: true, vertical: 'center' } };
notes.getRange('A1:F1').format.rowHeight = 28;
notes.getRange('A3:B8').format.rowHeight = 28;
notes.getRange('A:A').format.columnWidthPx = 48;
notes.getRange('B:B').format.columnWidthPx = 620;
notes.getRange('C:F').format.columnWidthPx = 20;

const preview = await workbook.render({
  sheetName: 'Summary',
  autoCrop: 'all',
  scale: 1,
  format: 'png',
});
await fs.mkdir(path.dirname(previewPath), { recursive: true });
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);

console.log(outputPath);
