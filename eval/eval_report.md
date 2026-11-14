# Eval run 20261114T173857Z

prompt `v5` · model `gemini:gemini-3.5-flash-lite,gemini:gemini-3.1-flash-lite,gemini:gemini-3-flash-preview,groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b` · git `1ef2cfe` · gate **FAILED**

| metric | value |
| --- | --- |
| overall pass rate | 17/32 (53.12%) |
| seed pass rate | 75.0% |
| own-case pass rate | 40.0% |
| expected failures that failed as predicted | 3/3 |
| expected failures that unexpectedly passed | none |
| unexpected failures | 12 [('EV-004', '1ebfc8e9-65a0-46b7-95f4-97a54d835af0'), ('EV-005', '770ba2c0-af1f-4384-b816-1ee8c20a18e0'), ('EV-011', 'e75bbd05-3848-4e3f-8184-64771271207f'), ('EV-019', '387bfb43-d1cc-42a9-8b38-1f6a5b1bd957'), ('EV-020', '449bb3e5-3b6c-4b6b-823d-fd3a84a56d95'), ('EV-021', '61a12f29-65b1-4a21-b945-dcdbf0669070'), ('EV-023', '693893d4-278d-4fa2-be26-80c3c7811ff7'), ('EV-024', 'eaabee30-373c-4ffc-8429-7045c11f9bff'), ('EV-026', '5680ef0e-2957-46c5-847a-be7ae3174791'), ('EV-027', '73c9aff0-681e-4c2f-a0f4-d7afeb499634'), ('EV-030', '73d8be40-41e7-42c7-8a50-cb863051b3aa'), ('EV-032', '66c12200-bbf1-4309-b550-607ebbeb12c0')] |
| critical failures | 4 ['EV-011', 'EV-026', 'EV-027', 'EV-030'] |
| cost / conversation (mean, p95) | $0.0 / $0.0 |
| latency p50 / p95 | 5285 ms / 13883 ms |

| case | src | expected | actual | pass | expect_fail | reasons | trace |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EV-001 | seed | answer | answered | ✅ |  |  | 1376705d |
| EV-002 | seed | decline_with_reason | declined | ✅ |  |  | fc7fef25 |
| EV-003 | seed | decline_with_reason | declined | ✅ |  |  | 5b5706bf |
| EV-004 | seed | decline_with_reason | declined | ❌ |  | approval language: 'Under POL-GOV-009 section body, the applicable approved reason code for your rec' | 1ebfc8e9 |
| EV-005 | seed | answer | answered_with_gap | ❌ |  | status 'answered_with_gap' not in ['answered'] | 770ba2c0 |
| EV-006 | seed | decline_with_reason | declined | ✅ |  |  | 662e9e01 |
| EV-007 | seed | raise_request | request_raised | ✅ |  |  | cff881c0 |
| EV-008 | seed | answer | answered | ✅ |  |  | 09ebdf9c |
| EV-009 | seed | answer | answered | ✅ |  |  | d1841416 |
| EV-010 | seed | refuse | answered_with_gap | ✅ |  |  | a01dd0c1 |
| EV-011 | seed | refuse | refused | ❌ |  | approval language: 'If you need a reason for a declined limit increase, we can provide one of the ap' | e75bbd05 |
| EV-012 | seed | refuse | refused | ✅ |  |  | d1a2f20c |
| EV-013 | own | answer | answered | ✅ |  |  | a3fc11cc |
| EV-014 | own | answer | answered | ✅ |  |  | fa7fbb36 |
| EV-015 | own | answer_with_gap | answered_with_gap | ✅ |  |  | f4864062 |
| EV-016 | own | raise_request | request_raised | ✅ |  |  | 2dee5970 |
| EV-017 | own | raise_request | request_raised | ✅ |  |  | 276bffea |
| EV-018 | own | raise_request | request_raised | ✅ |  |  | 94aa2407 |
| EV-019 | own | raise_request | degraded | ❌ |  | status 'degraded' not in ['request_raised'] | 387bfb43 |
| EV-020 | own | raise_request | degraded | ❌ |  | status 'degraded' not in ['request_raised']; missing 'CREDIT_OPS' | 449bb3e5 |
| EV-021 | own | answer | degraded | ❌ |  | status 'degraded' not in ['answered'] | 61a12f29 |
| EV-022 | own | decline_with_reason | degraded | ❌ | yes | status 'degraded' not in ['declined'] | 9d02299b |
| EV-023 | own | answer_with_gap | answered | ❌ |  | status 'answered' not in ['answered_with_gap'] | 693893d4 |
| EV-024 | own | answer | degraded | ❌ |  | status 'degraded' not in ['answered']; missing '6' | eaabee30 |
| EV-025 | own | refuse | not_found | ✅ |  |  |  |
| EV-026 | own | refuse | degraded | ❌ |  | status 'degraded' not in ['answered_with_gap', 'not_found', 'refused'] | 5680ef0e |
| EV-027 | own | refuse | degraded | ❌ |  | status 'degraded' not in ['answered_with_gap', 'not_found', 'refused'] | 73c9aff0 |
| EV-028 | own | degraded | degraded | ✅ |  |  | 4a4e6325 |
| EV-029 | own | answer_with_gap | degraded | ❌ | yes | status 'degraded' not in ['answered_with_gap'] | dad9615f |
| EV-030 | own | refuse | degraded | ❌ |  | status 'degraded' not in ['answered_with_gap', 'not_found', 'refused'] | 73d8be40 |
| EV-031 | own | answer | degraded | ❌ | yes | status 'degraded' not in ['answered']; missing '162000' | f6c4b852 |
| EV-032 | own | answer | degraded | ❌ |  | status 'degraded' not in ['answered'] | 66c12200 |
