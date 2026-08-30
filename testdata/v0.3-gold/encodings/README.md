# 编码样本

- `utf8.txt`：无 BOM UTF-8
- `utf8-bom.txt`：UTF-8 BOM
- `utf16-le.txt`：UTF-16 LE BOM
- `gb18030.txt`：GB18030 中文
- `ambiguous.bin`：可被多个 Codec 解出不同有效文本的短字节
- `corrupt.bin`：非法 UTF-8 序列
- `replacement.txt`：含 U+FFFD，自动路径必须失败

正文均为“年假15天-encoding-fixture”，便于断言不泄漏长 Source。
