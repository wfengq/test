现在请细读这一条，决定要不要推给他。

条目：{item}

你可以调用这些只读工具来读原文（每条最多 {per_item} 次，按需使用，够了就下结论）：
{tools}

每一步输出一个 JSON：
- 要用工具：{{"action": "tool", "tool": "工具名"}}
- 下结论：{{"action": "verdict", "decision": "pick" / "maybe" / "reject", "reason": "给自己的判断理由", "note_to_user": "写给他本人的一句话（80 字以内，说清楚这条对他有什么用；可以联系他最近标过有用的内容）", "no_source": true/false}}

如果最后没读到原文（没字幕、图片识别不出、详情打不开），只能凭简介和评论判断，必须把 no_source 设为 true。
