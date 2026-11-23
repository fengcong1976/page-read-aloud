# 自助点读助手

> 免费下载与图文教程：<https://www.mrheedu.com/apps/page-read-aloud>　·　更多免费软件：[www.mrheedu.com](https://www.mrheedu.com)
自助点读助手是一款把纸质书、练习册和 PDF 变成“会说话的书”的桌面工具。导入照片或 PDF 后，在画面上框选任意段落，程序先用本地 OCR 识别文字，再用在线神经语音朗读；支持中英文与多语种发音人、语速调节、整页连读和点读模式。内置 ECDICT 英汉、CC-CEDICT 汉英与成语词典，框选单词即可查看释义。图片和 PDF 只在本机处理、不会上传；朗读时仅将框选出的文字发送到微软语音服务合成音频。开源许可 AGPL-3.0，源码见 Gitee。

## 运行

```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python main.py
```

构建与安装包脚本见仓库内的 `.spec`、`.iss` 与 `build*.bat`。

## 许可

AGPL-3.0，详见 LICENSE。第三方组件声明见 THIRD_PARTY_NOTICES.md。

## 仓库说明

本仓库只包含源码，以下运行时资源体积较大，未纳入仓库：

- 词典数据未入库：ECDICT（MIT）、CC-CEDICT（CC BY-SA 4.0）、chinese-xinhua 成语数据（MIT），下载后生成 ecdict.db / cedict.db / idiom.db 放入项目根目录。

---

## 更多免费软件

本工具由深圳市创炫互动网络科技有限公司开发，官网提供免费安装包、图文教程，以及更多永久免费的 Windows 软件：**[www.mrheedu.com](https://www.mrheedu.com)**
