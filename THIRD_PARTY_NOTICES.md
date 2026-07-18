# Third-Party Notices

DUMB itself is licensed under the GNU General Public License version 3. See
[`LICENSE`](LICENSE). This file records notices for third-party components that
DUMB redistributes in its container image. It does not change those components'
licenses, grant trademark rights, or replace license files retained by package
managers and language runtimes.

## License and notice locations in the container

The image installs this file at:

```text
/usr/share/doc/dumb/THIRD_PARTY_NOTICES.md
```

DUMB also preserves license material supplied by its base image, operating
system packages, language runtimes, and Python packages. Common locations
include:

```text
/usr/share/doc/*/copyright
/usr/local/go/LICENSE
/usr/share/dotnet/LICENSE.txt
/usr/share/dotnet/ThirdPartyNotices.txt
*/site-packages/*.dist-info/licenses/
```

Those package-specific files remain authoritative for their corresponding
components and transitive dependencies.

---

## Apprise

- Project: <https://github.com/caronc/apprise>
- DUMB dependency range: `>=1.12.0,<2.0.0`
- License: BSD 2-Clause
- Upstream license: <https://github.com/caronc/apprise/blob/master/LICENSE>

```text
BSD 2-Clause License

Copyright (c) 2026, Chris Caron <lead2gold@gmail.com>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

---

## rclone

- Project: <https://github.com/rclone/rclone>
- License: MIT
- Upstream license: <https://github.com/rclone/rclone/blob/master/COPYING>
- Note: this notice was previously stored in DUMB's root `COPYING` file.

```text
Copyright (C) 2012 by Nick Craig-Wood http://www.craig-wood.com/nick/

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

---

## EnterpriseDB system_stats

- Project: <https://github.com/EnterpriseDB/system_stats>
- License: PostgreSQL-style permissive license
- Upstream license: <https://github.com/EnterpriseDB/system_stats/blob/master/LICENSE>

```text
Copyright (c) 2019 - 2020, EnterpriseDB Corporation

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement is
hereby granted, provided that the above copyright notice and this paragraph and
the following two paragraphs appear in all copies.

IN NO EVENT SHALL EnterpriseDB Corporation BE LIABLE TO ANY PARTY FOR DIRECT,
INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST
PROFITS, ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF
EnterpriseDB Corporation HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

EnterpriseDB Corporation SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING, BUT
NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE. THE SOFTWARE PROVIDED HEREUNDER IS ON AN "AS IS" BASIS, AND
EnterpriseDB Corporation HAS NO OBLIGATIONS TO PROVIDE MAINTENANCE, SUPPORT,
UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
```

---

## pgAdmin 4

- Project: <https://github.com/pgadmin-org/pgadmin4>
- License: PostgreSQL License
- Upstream license: <https://github.com/pgadmin-org/pgadmin4/blob/master/LICENSE>

```text
pgAdmin 4
=========

This software is released under the PostgreSQL licence.

-------------------------------------------------------------------------------
Copyright (C) 2013 - 2026, The pgAdmin Development Team

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement is
hereby granted, provided that the above copyright notice and this paragraph and
the following two paragraphs appear in all copies.

IN NO EVENT SHALL THE PGADMIN DEVELOPMENT TEAM BE LIABLE TO ANY PARTY FOR
DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST
PROFITS, ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF
THE PGADMIN DEVELOPMENT TEAM HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

THE PGADMIN DEVELOPMENT TEAM SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING,
BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE. THE SOFTWARE PROVIDED HEREUNDER IS ON AN "AS IS" BASIS, AND
THE PGADMIN DEVELOPMENT TEAM HAS NO OBLIGATIONS TO PROVIDE MAINTENANCE, SUPPORT,
UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
```

---

## Components requiring redistribution-permission review

As of July 17, 2026, the following upstream repositories used to produce or
supply files in the DUMB image did not publish a license or notice file on their
default branch:

- Zilean: <https://github.com/iPromKnight/zilean>
- dmbdb: <https://github.com/nicocapalbo/dmbdb>
- cli_debrid: <https://github.com/godver3/cli_debrid>
- zurg-testing configuration and script files:
  <https://github.com/debridmediamanager/zurg-testing>

Public source availability alone is not a redistribution license. This section
does not grant permission to use, modify, or redistribute those works. DUMB
maintainers should retain written permission from the applicable copyright
holders or request that upstream publish an appropriate license. Once a license
is published or permission terms are documented, add the applicable notice to
this file.
