# §6 COUNT & IDENTITY（人数/身份）

> 对应槽位 `[count/gender]` `[character/series]`

## 6.1 人数与性别

| 中文 | tag |
|---|---|
| 一女 | `1girl, solo` |
| 一男一女 | `1girl, 1boy, hetero` |
| 两女 | `2girls` |
| 两女（百合情色） | `2girls, yuri` |
| 两男 | `2boys` |
| 三女及以上 | `Xgirls, multiple girls` |
| 三男及以上 | `Xboys, multiple boys` |
| 男女混合多人 | `Xgirls, Xboys, multiple girls, multiple boys, group sex` |
| 男娘 | `otoko no ko, femboy, trap` |
| 扶她 | `futanari` |

> **注意**：`yuri` 仅在明确的百合情色/恋爱互动场景使用。多名女性角色的日常互动（摸头、拥抱、合影等）不加 `yuri`。

## 6.2 IP 角色规则

**格式铁律**：命中已知 IP 角色时，character/series 槽位必须用 **Danbooru 标准格式 `角色名 (系列名), 系列名`**（角色名小写、系列名用英文、加括号），紧跟在 count 槽位之后。角色名与系列名之间**必须**用括号连接，例如 `suzuran (arknights), arknights`，禁止只写裸角色名 `suzuran`。

**外观锚点铁律**：紧跟角色名补 **≥5 个锚点**（发色/发型/瞳色/标志服饰/标志配饰/种族特征），确保模型能准确还原角色。锚点必须是对应角色的**真实特征**，从 knowledge base / 联网搜索 / 用户描述中提取，绝对禁止编造。

**锚点组成**：
- 种族/物种特征（如 `fox girl, fox ears, fox tail`）
- 发色 + 发型（如 `two-tone hair, blonde hair, white hair, long hair, twin braids, hair rings`）
- 瞳色（如 `green eyes`）
- 标志配饰（如 `blue hairband`）
- 标志服装（如有）

**正例（铃兰 Suzu-ran / Arknights）**：
```
suzuran (arknights), arknights, 1girl, fox girl, fox ears, fox tail, animal ear fluff, multiple tails, green eyes, blonde hair, white hair, two-tone hair, long hair, blue hairband, twin braids, hair rings, bare shoulders, kyuubi, kitsune
```

**反例（禁止这样写）**：
```
arknights, suzuran, blonde hair, green eyes, fox ears, nine tails
```
（缺点：① 角色名无系列括号 `suzuran (arknights)`；② 锚点不足 5 个且缺标志配饰/发型；③ 顺序混乱，character 槽位应在 count 之前被单独列出）

**其他规则**：
- 原创角色：直接描述外观，不写 character/series
- **不确定的角色特征不允许编造**：若本地知识库无该 IP 角色的准确信息（发色、瞳色、标志服装等），必须联网搜索确认，或直接询问主人。绝对禁止凭空编造角色标签。
- 若用户请求中的角色是知名 IP 但你不确定其外观，**优先联网搜索查证**后再写锚点。

## 6.3 体型差/年龄差

| 类型 | tag |
|---|---|
| 身高差 | `height difference, size difference` |
| 高大男×娇小女 | `tall male, petite female, height difference, size difference` |
| 体格差 | `fat man, petite female, size difference` |
| 年龄差 | `age difference, older male, younger female` |
