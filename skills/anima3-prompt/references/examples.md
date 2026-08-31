# §15 EXAMPLES（完整跑图案例）

> 每个案例 = 中文场景描述 + 完整英文 prompt + 简短推理注释。所有 prompt 均**不含质量词**（脚本会注入 masterpiece/best quality），全部 lowercase、逗号+空格分隔、单行、按 §4 槽位顺序组装。LLM 生成新提示词时**参考这些案例的标签选择逻辑**。

## 15.1 IP 角色单人展示（铃兰 / Arknights）

**中文描述**：铃兰（明日方舟）一个人，狐狸耳朵和尾巴，双马尾，蓝色发带，站在樱花树下回眸。

**prompt**：
```
suzuran (arknights), arknights, 1girl, solo, fox girl, fox ears, fox tail, multiple tails, green eyes, blonde hair, white hair, two-tone hair, long hair, twin braids, hair rings, blue hairband, flower hair ornament, white dress, standing, turning around, direct eye contact, over shoulder, sakura tree, petals falling, soft daylight, bloom
```

**推理**：角色名带系列括号 `suzuran (arknights), arknights` → 外观锚点（狐耳/狐尾/双色发/双马尾/发环/蓝色发带）→ 服装 → 动作 → 镜头（回眸 over shoulder）→ 场景 → 氛围。

## 15.2 原创单人展示（原创女角色）

**中文描述**：一位银发红瞳的精灵少女，穿着薄纱连衣裙，在月光下的古堡露台独自站立，望向远方。

**prompt**：
```
1girl, solo, silver hair, long hair, red eyes, pointy ears, elf, thin dress, sheer clothing, nightgown, standing, looking away, from side, profile, castle balcony, night, moonlight, full moon, elegant, mysterious
```

**推理**：原创角色不写 character/series，直接从 appearance（银发/红瞳/尖耳/精灵）→ clothing → pose → camera → scene → detail。

## 15.3 双人正戏-传教士（原创）

**中文描述**：一男一女在床上，男上女下，传教士体位，女方双腿缠绕男方腰部，表情迷离，房间灯光柔和。

**prompt**：
```
1girl, 1boy, hetero, missionary, lying on back, legs wrapped around partner, on top, male on top, vaginal penetration, penis, breasts, blush, open mouth, half-closed eyes, pleasure, sweat, from side, bedroom, bed, bed sheet, dim lighting, intimate
```

**推理**：count（1girl, 1boy, hetero）→ pose（missionary + 腿缠绕）→ 动作细节 → expression → camera → scene。

## 15.4 双人前戏-口交

**中文描述**：一对情侣，女方跪在男方身前为他口交，一只手握住根部，抬头用眼睛看着男方。

**prompt**：
```
1girl, 1boy, hetero, fellatio, kneeling, looking up at partner, deep throat, hand on penis, eye contact, blush, tears, wet eyes, saliva, open mouth, from front, pov, bedroom, dim lighting, intimate
```

**推理**：count → pose（fellatio + kneeling）→ 表情（eye contact/blush/tears）→ camera（pov）→ scene。

## 15.5 多人/群交

**中文描述**：三个女生和一个男生在酒店房间的大床上群交，画面复杂，需要区分每个人。

**prompt**：
```
3girls, 1boy, group sex, multiple girls, multiple boys, threesome, foursome, naked, intertwined, missionary, cowgirl position, fellatio, from above, wide shot, hotel room, large bed, white sheets, messy, chaotic, pornographic
```

**推理**：count 精确（3girls, 1boy）→ 群交标签 → 体位组合 → camera（from above, wide shot 容纳多人）→ scene。

## 15.6 特殊主题-NTR（他人妻子）

**中文描述**：熟女被丈夫以外的男人从后面进入，她羞耻地看向镜头，丈夫在旁边被绑住无法阻止。

**prompt**：
```
1girl, 1boy, mature female, doggystyle, vaginal penetration, male on top, from behind, looking at viewer, blush, ashamed, embarrassed, tears, bound male, cuckold, netorare, bedroom, bed, dim lighting, intimate, as if aware of being watched by the viewer
```

**推理**：跨槽位配方（themes-a NTR）→ 身份/动作/表情组合，末尾自然语言补充观众叙事关系。

## 15.7 特殊主题-另类日常（宠物化）

**中文描述**：少女像宠物一样戴着项圈和铃铛，四肢着地趴在地上，尾巴摇动，主人伸手轻抚她的头。

**prompt**：
```
1girl, animal ear fluff, fox ears, fox tail, tail wagging, collar, bell, pet play, petting, on all fours, crawling, happy, open mouth, tongue out, blush, looking at viewer, living room, tatami, soft lighting, playful, domestic
```

**推理**：主题配方（另类日常宠物化）→ 配饰（collar/bell）→ 动作（on all fours/crawling）→ 表情（happy/tongue out）→ 场景。

---

**通用要点**：
- 涉及已知 IP 角色：必带 `角色名 (系列名), 系列名` 且 ≥5 个外观锚点（见 §6.2）。
- 原创角色：直接外观，不写 character/series。
- 标签总数单人 16-30 / 双人 22-38 / 复杂 30-48（见 §4.2）。
- 始终不含质量词（脚本注入）、画师名、光线/光影/色调标签（lora 内置）。
