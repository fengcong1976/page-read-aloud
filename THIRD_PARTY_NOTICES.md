# 书页朗读助手 — 第三方组件与许可声明

本应用整体以 **AGPL-3.0** 发布（见 LICENSE）。源码与构建脚本：
https://gitee.com/FC1976/page-read-aloud
GitHub 镜像：https://github.com/fengcong1976/page-read-aloud

| 组件 | 许可 | 来源 / 源码 |
|---|---|---|
| PyQt6 6.11.0 | GPL-3.0 | https://riverbankcomputing.com/software/pyqt/ |
| PyMuPDF 1.28.2 | AGPL-3.0 | https://pymupdf.readthedocs.io/ |
| RapidOCR / PP-OCR 模型 | Apache-2.0 | https://github.com/RapidAI/RapidOCR |
| onnxruntime | MIT | https://github.com/microsoft/onnxruntime |
| OpenCV | Apache-2.0 | https://opencv.org/ |
| NumPy | BSD-3-Clause | https://numpy.org/ |
| Pillow 12.3.0 | HPND | https://python-pillow.org/ |
| wordninja | MIT | https://github.com/keredson/wordninja |
| edge-tts 7.2.8 | LGPL-3.0（srt_composer.py 为 MIT） | https://github.com/rany2/edge-tts |
| ECDICT | MIT | https://github.com/skywind3000/ECDICT |
| CC-CEDICT | CC BY-SA 4.0 | https://cc-cedict.org/ |
| chinese-xinhua 成语数据 | MIT | https://github.com/pwxcoo/chinese-xinhua |

> PyMuPDF 采用 AGPL-3.0，因此本应用整体按 AGPL-3.0 发布；分发二进制时必须同时提供对应源码。

> 朗读使用在线微软语音服务：仅框选出的文字会发送到服务端合成音频，图片与 PDF 文件不上传；
> 程序不随包分发任何预生成语音。首次运行会提示并征得同意。

> 词典数据：ECDICT（MIT）、CC-CEDICT（CC BY-SA 4.0，修改后的词库按同许可共享）、
> chinese-xinhua 成语数据（MIT）。

## 许可全文

安装目录下的 `LICENSES/` 存放 AGPL-3.0、LGPL-3.0、Apache-2.0、CC BY-SA 4.0、edge-tts 与 wordninja 的许可全文。
