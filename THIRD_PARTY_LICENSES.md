# Third-Party Licenses

WinForge is built with the help of the following open-source libraries.
This notice is provided to comply with their license terms.

---

## PySide6 (Qt for Python)

- **License:** GNU Lesser General Public License v3 (LGPLv3)
- **Copyright:** © The Qt Company Ltd. and other contributors
- **Project home:** https://www.qt.io/qt-for-python
- **Source code:** https://code.qt.io/cgit/pyside/pyside-setup.git/ (and https://download.qt.io/official_releases/QtForPython/)
- **Full license text:** https://www.gnu.org/licenses/lgpl-3.0.html

WinForge uses PySide6 to build its graphical interface. PySide6/Qt is
**not modified** — it is used as an unmodified, dynamically-loaded library.

**How this build stays LGPL-compliant:** The Windows `.exe` is built with
PyInstaller in `--onefile` mode. At runtime, PyInstaller extracts the
Qt/PySide6 shared libraries (`.dll` files) to a temporary folder and loads
them **dynamically** — they are not statically compiled into the
application. This preserves the LGPL's core requirement that the licensed
library remain swappable/replaceable by the end user, since the extracted
`.dll` files are ordinary dynamic libraries on disk, not code fused
permanently into the executable.

Full PySide6/Qt source code is publicly available from the official Qt
project at the links above, at no cost, for anyone who wishes to inspect,
modify, or rebuild it.

*Note: this explanation reflects the commonly-accepted interpretation used
by many PyInstaller + PySide6/PyQt projects. It is not a legal opinion. If
you need a legally certain compliance determination (e.g. for commercial
redistribution at scale), consult a qualified lawyer familiar with LGPL and
software licensing.*

---

## psutil

- **License:** BSD 3-Clause License
- **Copyright:** © 2009, Giampaolo Rodola. All rights reserved.
- **Project home:** https://github.com/giampaolo/psutil

```
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.
3. Neither the name of the psutil authors nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

---

## WinForge's own license

WinForge's own source code (everything outside of the third-party
libraries listed above) is licensed under the MIT License — see
[`LICENSE`](LICENSE) in the root of this repository.
