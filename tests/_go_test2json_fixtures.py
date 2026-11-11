# The event streams below are copied, unmodified, from the Go project's
# cmd/internal/test2json testdata (the *.json files) at the go1.25.13 release.
#
# Copyright 2009 The Go Authors.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#    * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#    * Neither the name of Google LLC nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""Go test2json reference streams (go1.25.13 testdata), one string per file."""

from __future__ import annotations

PANIC = r"""{"Action":"start"}
{"Action":"output","Test":"TestPanic","Output":"--- FAIL: TestPanic (0.00s)\n"}
{"Action":"output","Test":"TestPanic","Output":"panic: oops [recovered]\n"}
{"Action":"output","Test":"TestPanic","Output":"\tpanic: oops\n"}
{"Action":"output","Test":"TestPanic","Output":"\n"}
{"Action":"output","Test":"TestPanic","Output":"goroutine 7 [running]:\n"}
{"Action":"output","Test":"TestPanic","Output":"testing.tRunner.func1(0xc000092100)\n"}
{"Action":"output","Test":"TestPanic","Output":"\t/go/src/testing/testing.go:874 +0x3a3\n"}
{"Action":"output","Test":"TestPanic","Output":"panic(0x1110ea0, 0x116aea0)\n"}
{"Action":"output","Test":"TestPanic","Output":"\t/go/src/runtime/panic.go:679 +0x1b2\n"}
{"Action":"output","Test":"TestPanic","Output":"command-line-arguments.TestPanic(0xc000092100)\n"}
{"Action":"output","Test":"TestPanic","Output":"\ta_test.go:6 +0x39\n"}
{"Action":"output","Test":"TestPanic","Output":"testing.tRunner(0xc000092100, 0x114f500)\n"}
{"Action":"output","Test":"TestPanic","Output":"\tgo/src/testing/testing.go:909 +0xc9\n"}
{"Action":"output","Test":"TestPanic","Output":"created by testing.(*T).Run\n"}
{"Action":"output","Test":"TestPanic","Output":"\tgo/src/testing/testing.go:960 +0x350\n"}
{"Action":"fail","Test":"TestPanic"}
{"Action":"output","Output":"FAIL\tcommand-line-arguments\t0.042s\n"}
{"Action":"output","Output":"FAIL\n"}
{"Action":"fail"}
"""

TIMEOUT = r"""{"Action":"start"}
{"Action":"run","Test":"Test"}
{"Action":"output","Test":"Test","Output":"=== RUN   Test\n"}
{"Action":"output","Test":"Test","Output":"panic: test timed out after 1s\n"}
{"Action":"output","Test":"Test","Output":"\n"}
{"Action":"output","Output":"FAIL\tp\t1.111s\n"}
{"Action":"output","Output":"FAIL\n"}
{"Action":"fail"}
"""

BENCHFAIL = r"""{"Action":"start"}
{"Action":"output","Test":"BenchmarkFoo","Output":"--- FAIL: BenchmarkFoo\n"}
{"Action":"output","Test":"BenchmarkFoo","Output":"\tx_test.go:8: My benchmark\n"}
{"Action":"fail","Test":"BenchmarkFoo"}
{"Action":"output","Output":"FAIL\n"}
{"Action":"output","Output":"FAIL\tcommand-line-arguments\t0.008s\n"}
{"Action":"fail"}
"""

ISSUE23920 = r"""{"Action":"start"}
{"Action":"run","Test":"TestWithColons"}
{"Action":"output","Test":"TestWithColons","Output":"=== RUN   TestWithColons\n"}
{"Action":"run","Test":"TestWithColons/[::1]"}
{"Action":"output","Test":"TestWithColons/[::1]","Output":"=== RUN   TestWithColons/[::1]\n"}
{"Action":"run","Test":"TestWithColons/127.0.0.1:0"}
{"Action":"output","Test":"TestWithColons/127.0.0.1:0","Output":"=== RUN   TestWithColons/127.0.0.1:0\n"}
{"Action":"output","Test":"TestWithColons","Output":"--- PASS: TestWithColons (0.00s)\n"}
{"Action":"output","Test":"TestWithColons/[::1]","Output":"    --- PASS: TestWithColons/[::1] (0.00s)\n"}
{"Action":"pass","Test":"TestWithColons/[::1]"}
{"Action":"output","Test":"TestWithColons/127.0.0.1:0","Output":"    --- PASS: TestWithColons/127.0.0.1:0 (0.00s)\n"}
{"Action":"pass","Test":"TestWithColons/127.0.0.1:0"}
{"Action":"pass","Test":"TestWithColons"}
{"Action":"output","Output":"PASS\n"}
{"Action":"pass"}
"""

ATTR = r"""{"Action":"start"}
{"Action":"run","Test":"TestAttr"}
{"Action":"output","Test":"TestAttr","Output":"=== RUN   TestAttr\n"}
{"Action":"attr","Test":"TestAttr","Key":"key","Value":"value"}
{"Action":"output","Test":"TestAttr","Output":"=== ATTR  TestAttr key value\n"}
{"Action":"run","Test":"TestAttr/sub"}
{"Action":"output","Test":"TestAttr/sub","Output":"=== RUN   TestAttr/sub\n"}
{"Action":"attr","Test":"TestAttr/sub","Key":"key","Value":"value"}
{"Action":"output","Test":"TestAttr/sub","Output":"=== ATTR  TestAttr/sub key value\n"}
{"Action":"output","Test":"TestAttr","Output":"--- PASS: TestAttr (0.00s)\n"}
{"Action":"output","Test":"TestAttr/sub","Output":"    --- PASS: TestAttr/sub (0.00s)\n"}
{"Action":"pass","Test":"TestAttr/sub"}
{"Action":"pass","Test":"TestAttr"}
{"Action":"output","Output":"PASS\n"}
{"Action":"pass"}
"""
