/**
 * 测试数据文件生成脚本
 * 用于创建 PDF 和 Excel 测试文件
 */

const XLSX = require('xlsx');
const fs = require('fs');
const path = require('path');

// ========== Excel 测试文件 ==========

function createTestExcel() {
  const workbook = XLSX.utils.book_new();

  // Sheet 1: 普通数据表
  const sheet1Data = [
    ['姓名', '部门', '工号', '入职日期', '薪资'],
    ['张三', '产品部', 'EMP001', '2023-01-15', 15000],
    ['李四', '研发部', 'EMP002', '2023-03-20', 20000],
    ['王五', '设计部', 'EMP003', '2023-06-01', 18000],
    ['赵六', '测试部', 'EMP004', '2024-01-10', 16000],
    ['钱七', '运维部', 'EMP005', '2024-02-28', 17000],
  ];
  const sheet1 = XLSX.utils.aoa_to_sheet(sheet1Data);
  XLSX.utils.book_append_sheet(workbook, sheet1, '员工信息');

  // Sheet 2: 包含公式的表
  const sheet2Data = [
    ['产品', '单价', '数量', '小计', '折扣', '实付'],
    ['商品A', 100, 5, { f: 'B2*C2' }, 0.9, { f: 'D2*E2' }],
    ['商品B', 200, 3, { f: 'B3*C3' }, 0.85, { f: 'D3*E3' }],
    ['商品C', 50, 10, { f: 'B4*C4' }, 0.95, { f: 'D4*E4' }],
    ['', '', '', '', '合计:', { f: 'SUM(F2:F4)' }],
  ];
  const sheet2 = XLSX.utils.aoa_to_sheet(sheet2Data);
  XLSX.utils.book_append_sheet(workbook, sheet2, '订单计算');

  // Sheet 3: 空 Sheet（边界测试）
  const sheet3 = XLSX.utils.aoa_to_sheet([]);
  XLSX.utils.book_append_sheet(workbook, sheet3, '空表');

  // 写入文件
  const outputPath = path.join(__dirname, 'test-data.xlsx');
  XLSX.writeFile(workbook, outputPath);
  console.log('Excel 测试文件已创建:', outputPath);
}

// ========== CSV 备份（用于简单测试） ==========

function createTestCSV() {
  const csvContent = `姓名,部门,工号,入职日期,薪资
张三,产品部,EMP001,2023-01-15,15000
李四,研发部,EMP002,2023-03-20,20000
王五,设计部,EMP003,2023-06-01,18000
赵六,测试部,EMP004,2024-01-10,16000
钱七,运维部,EMP005,2024-02-28,17000`;

  const outputPath = path.join(__dirname, 'test-data.csv');
  fs.writeFileSync(outputPath, csvContent, 'utf-8');
  console.log('CSV 测试文件已创建:', outputPath);
}

// ========== 执行 ==========

try {
  createTestExcel();
  createTestCSV();
  console.log('\n测试数据文件准备完成！');
} catch (error) {
  console.error('创建测试文件失败:', error.message);
  // 如果 xlsx 模块不可用，只创建 CSV
  createTestCSV();
  console.log('\n(xlsx 模块不可用，仅创建了 CSV 文件)');
}
