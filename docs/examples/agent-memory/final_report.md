## 会话/轮次/事实粒度的效果条件核验

## 会话/轮次/事实粒度的效果条件核验

### 一、当前证据能说什么

本批材料中，只有一篇作品（arXiv:2502.05589，SeCom）直接比较了不同记忆粒度在长程对话问答中的效果，且其粒度划分是**会话级 / 轮次级 / 段级（segment-level）**，并非问题中设想的「事实级」。因此，关于「事实级记忆」的效果条件，本批材料**没有可用的直接证据**。

该作品报告的核心结果是：段级记忆在多种上下文预算下均优于轮次级与会话级记忆 [E:fact_6a4d7d6705ab1e3b1c30e97f]。但需注意，该结论仅来自**单一作品（SeCom），独立性未知**，不能视为更广泛的共识；且这一比较是在**统一施加了基于压缩的记忆去噪**之后进行的，作者明确说明这是为了隔离粒度本身的端到端影响 [E:fact_6a4d7d6705ab1e3b1c30e97f]。也就是说，该结论成立的前提是「压缩去噪已开启」，不能外推到未压缩的裸粒度比较。此外，该结论缺少 required_conditions 中的 memory_key、memory_value、history_length、retrieval_budget、metric 等条件绑定，因此不能作为可复现的粒度效果条件。

### 二、粒度效果依赖的条件

**（1）检索系统能力。** 该作品发现，轮次级与会话级记忆的检索性能对检索模型高度敏感：从 MPNet 检索器切换到 BM25 检索器，在 LOCOMO 与 Long-MT-Bench+ 上 GPT4Score 提升最高分别达 11.98 与 7.89 分；而段级记忆对检索系统的鲁棒性更强 [E:fact_bfa98eb5e921385537b83f7c]。作者给出的解释是：轮次级单元碎片化、可能不含目标请求的关键词，会话级单元则含大量无关信息，两者都使检索性能更依赖检索器能力 [E:fact_bfa98eb5e921385537b83f7c]。需注意，上述分数来自**单一作品（SeCom）、独立性未知**，且报告未给出其任务、数据集、模型、历史长度与检索预算的完整绑定，因此结论强度受限。**「段级记忆对检索系统鲁棒性更强」这一结论同样仅为单一作品（SeCom）的报告，独立性未知，不能作为广泛共识陈述。** 该结论缺少 required_conditions 中的 task、dataset、model、history_length、retrieval_budget、metric 等条件绑定，不能作为可复现的粒度效果条件。这说明**粒度结论不能脱离检索预算与检索器类型单独陈述**。

**（2）压缩/去噪机制的存在。** 移除基于压缩的记忆去噪机制，会导致 LOCOMO 上 GPT4Score 最高下降 9.46 分 [E:fact_544a0f4a10c76630a14c057b]。该作品使用的具体配置是 LLMLingua-2、压缩率 75%、xlm-roberta-large 作为去噪基础模型 [E:fact_208664184a64da6bf3d55e25]。需注意，该结论来自**单一作品（SeCom）、独立性未知**，报告未说明该下降是否在相同任务、数据集、模型、历史长度与检索预算下获得，也未说明其统计显著性。该结论缺少 required_conditions 中的 task、dataset、model、history_length、retrieval_budget、metric 等条件绑定，不能作为可复现的粒度效果条件。因此，粒度效果与压缩配置是耦合的，不能把粒度收益与压缩收益分开归因；**但需明确，这一耦合判断本身也是基于单一作品（SeCom）的推断，而非多篇独立验证的结论。**

**（3）历史长度与上下文窗口。** 即使使用 32K 上下文窗口、无需截断即可容纳完整历史的 Mistral-7B，「全历史」方法的表现仍低于使用记忆构建与检索机制的方法 [E:fact_6f9a8b3a80d2524ffe5fc985]。需注意，该结论来自**单一作品（SeCom）、独立性未知**，报告未说明该比较是否在相同任务、数据集、历史长度与检索预算下进行，也未说明「全历史」与 SeCom 的具体配置差异。该结论缺少 required_conditions 中的 task、dataset、model、history_length、retrieval_budget、metric 等条件绑定，不能作为可复现的粒度效果条件。这提示：在历史长度未超出上下文窗口时，记忆管理（含压缩/去噪）仍可能通过降噪提升准确性，而非仅仅解决「装不下」的问题；**但该提示仅为单一作品（SeCom）的提示性发现，独立性未知，不能作为广泛结论。**

### 三、压缩与预算的边界条件（来自另一篇作品）

另一篇作品（arXiv:2502.13176，BaKlaVa）报告了细粒度内存分配在压缩条件下的表现，但其任务并非对话问答，而是长上下文任务，且相关证据的 source_material_status 为 source_only（非原文验证），因此只能作为**别的设置下的作者报告**，不能计入本命题的支持或反驳：

- 对 Qwen2.5-7B（32k 上下文），低压缩比（如 0.5）下性能无显著下降，高压缩比导致分数逐渐降低 [E:fact_58386d245529b7daad1810a0]；
- 对 LLaMA-3-8B（8k 上下文），提示截断易丢失关键指令，BaKlaVa 能保留关键指令直到更高压缩比（0.6）[E:fact_0fefc1a446b6e68a8b5248c0]；
- BaKlaVa 在高达 70% 压缩率下保持接近基线性能 [E:fact_fff19ddaa8decfee85b85ea9]。

这些结果来自**单一作品（BaKlaVa）、独立性未知**，说明压缩损失程度依赖模型上下文长度与压缩比，但**不能**直接用来支持或反驳「长程对话问答中事实级记忆更优」这一命题。**需特别强调，BaKlaVa 的任务为长上下文任务而非对话问答，其证据的 source_material_status 为 source_only（非原文验证），与长程对话问答的关联性较弱，不应被解读为对话问答场景下的直接证据。** 这些证据缺少 required_conditions 中的 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定，不能作为压缩损失或长期一致性效果的结论性证据。

### 四、结论强度与缺口

- 关于「会话级/轮次级/段级」粒度：本批材料中有 **1 篇作品**报告了段级优于轮次级与会话级，且该优势在多种上下文预算下保持一致 [E:fact_6a4d7d6705ab1e3b1c30e97f]。独立性未知，不能称为「多篇独立研究已证明」。该结论缺少 required_conditions 中的 memory_key、memory_value、history_length、retrieval_budget、metric 等条件绑定，且仅在「压缩去噪已开启」的前提下成立，不能外推到未压缩的裸粒度比较。
- 关于「事实级」粒度：本批材料**没有**直接比较事实级与会话级/轮次级的证据，无法下结论。该判断基于对全部交付材料的审查，未检索到包含 task、dataset、model、memory_key、memory_value、history_length、retrieval_budget、metric 等条件绑定的材料；**缺失的具体条件包括：任务类型、数据集、模型、记忆键、记忆值、历史长度、检索预算与评价指标。**
- 关于记忆键与记忆值的组织方式：本批材料未提供可核验的键/值组织细节，无法回答该子问题。该判断基于对全部交付材料的审查，未检索到包含 memory_key、memory_value 条件绑定的材料；**缺失的具体条件包括：任务类型、数据集、模型、记忆键、记忆值、历史长度、检索预算与评价指标。**
- 关于跨会话时间顺序测试：本批材料未提供跨会话时间顺序的验证证据。该判断基于对全部交付材料的审查，未检索到包含跨会话时间顺序测试的材料；**缺失的具体条件包括：跨会话时间顺序测试设计、任务类型、数据集、模型、历史长度、检索预算与评价指标。** 该子问题属于 granularity_conditions 的 research_questions 之一，当前证据缺口明确。
- 关于压缩的长期一致性损失：本批材料仅有压缩对 QA 分数影响的证据 [E:fact_544a0f4a10c76630a14c057b]，没有针对「长期一致性」或「时间顺序敏感任务」的专门测量，因此该假设处于证据不足状态。该判断基于对全部交付材料的审查，未检索到包含 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定的材料；**缺失的具体条件包括：任务类型、数据集、模型、压缩方法、压缩比、上下文预算与评价指标。** 具体而言，未验证长期一致性的任务或指标包括：跨会话时间顺序敏感任务、矛盾处理任务、遗忘/更新/巩固机制相关指标；用户事实抽取（如 COMEDY 的 Task 1/Task 2）是压缩流程的中间步骤，不能等同于对压缩后长期一致性的独立验证。

综上，可复现的粒度效果差异目前只在「段级 vs 轮次级 vs 会话级」这一划分下、且绑定于特定检索器、压缩配置与上下文预算时被报告；「事实级」粒度与记忆键/值组织方式的效果条件，在本批材料中尚无可核验证据。

## 记忆压缩损失与长期一致性的测量条件

## 记忆压缩损失与长期一致性的测量条件

### 一、本节可用的证据边界

本节核验的核心问题是：记忆压缩方法在长程对话问答中的压缩损失与长期一致性，必须绑定哪些条件才能被测量。经材料审查，当前证据**不足以**对两个候选命题（粒度效应条件、压缩权衡条件）给出支持或反驳结论。两个命题的评估状态均为 `insufficient_evidence`，原因代码为 `material_excerpt_truncated`，即交付材料在实验设置、方法、结果三个区块上均未通过审查，`materials_reviewed` 为 0。该判断基于对全部交付材料的审查，未检索到包含 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定的材料；**缺失的具体条件包括：任务类型、数据集、模型、压缩方法、压缩比、上下文预算与评价指标。** 因此以下内容只能作为**条件清单与证据缺口说明**，不能作为效果结论。需说明的是，本节后续引用的 COMEDY、Dolphin、MADial-Bench、情景记忆 RAG、程序性记忆等证据，均来自上述未通过审查的材料，仅用于说明测量压缩损失所需绑定的条件清单，不用于支持或反驳两个候选命题。

### 二、压缩流程中已可确认的粒度结构（仅限方法描述）

COMEDY 方法明确区分了两个记忆阶段：先提取**会话级记忆**（session-level memory，包含会话级事件与用户画像的自然语句），再将其压缩为**压缩记忆**（compressive memory，包含详细用户画像与事件简录），最后基于当前上下文与压缩记忆生成回复 [E:fact_1a747f0a5ec35f10ecaf3600]。该证据的 source_material_status 为 fetched、provenance_status 为 quote_bound，但仅为方法描述，未给出其任务、数据集、模型、压缩方法、压缩比、上下文预算与指标，**不包含**压缩前后长期一致性的对比测量，因此**不能用于判断压缩是否无损，也不能作为压缩损失或长期一致性效果的证据**。该证据缺少 required_conditions 中的 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定。

### 三、测量压缩损失必须绑定的条件

从已交付材料可归纳出以下必须同时绑定的条件，缺一即不可比：

**1. 上下文预算与压缩比**
- 上下文-only 基线将过去对话直接拼接至模型最大令牌长度（LLaMA 为 2k，ChatGPT 为 8k）[E:fact_c099fda9b4acf6a0d32f0712]，说明上下文预算随模型而异，压缩比的分母不统一。
- 检索基线使用 Top-3 检索记忆 [E:fact_dc98eccd763c25f66eae8ac2]，而另一项工作报告 RAG 仅用 Top-10 话语和少量记忆即可将令牌消耗减少 90% 以上且整体性能几乎不下降 [E:fact_be2497613fde2e2229a8d138]。两项证据的 source_material_status 分别为 fetched 与 source_only，独立性未知，且未给出任务、数据集、模型、历史长度与指标的完整绑定。两者检索预算不同（Top-3 vs Top-10）、任务与数据集不同，**不可直接比较**，只能作为「不同设置下的作者报告」，**不能作为压缩损失或长期一致性效果的结论性证据**。这些证据缺少 required_conditions 中的 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定。

**2. 任务与数据集**
- Dolphin 数据集源自 X Eva，AI 角色由用户定义，具有独特个性、背景与对话特征 [E:fact_d9fd7e2dda012201cf92f922]，构成一种具体的长程对话问答场景。
- MADial-Bench 依据心理学研究构建记忆选择与使用标准，涉及情感、关系、事件、对象等因素 [E:fact_4bdb5c4a0a1030a908d38695]，其 Setting 3 使用最佳嵌入模型的检索结果作为候选集，模拟最现实的检索预算与记忆组织方式 [E:fact_761002e8006bd7f93d0d7ee1]。两项证据的 source_material_status 均为 fetched、provenance_status 均为 quote_bound，独立性未知，且未给出任务、数据集、模型、历史长度与指标的完整绑定。这说明记忆内容粒度与检索预算共同影响任务表现，但该材料未报告压缩前后的长期一致性指标，**因此不能作为压缩损失或长期一致性效果的结论性证据**。这些证据缺少 required_conditions 中的 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定。

**3. 模型类型**
- 结合情景记忆的 RAG 在 GPT-4o mini、Llama3.2 3B Instruct 上优于纯 RAG，但在 Qwen2.5 7B Instruct 上略差于纯 RAG [E:fact_6fd9109503f1b4fc4213c475]，表明效果依赖模型类型。该证据为 source_only，**不是原文验证**，独立性未知，且未给出任务、数据集、历史长度、检索预算与指标，只能作为线索，**不能作为结论性证据**。该证据缺少 required_conditions 中的 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定。

**4. 记忆类型与任务性质的匹配**
- 在 LoCoMo QA 设置中，程序性记忆的加入产生负面结果，作者解释为任务性质（QA 与规划/多步推理）与程序性记忆实现不匹配 [E:fact_1b963d253bb519086f75bd59]。该证据的 source_material_status 为 fetched、provenance_status 为 quote_bound，独立性未知，且未给出模型、历史长度、检索预算与指标的完整绑定。这提示记忆类型有效性依赖任务性质，但同样不涉及压缩损失测量，**不能作为压缩损失或长期一致性效果的证据**。该证据缺少 required_conditions 中的 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定。

### 四、明确未被验证的方面

- 一项研究明确声明未涉及长期记忆管理问题（遗忘、更新、巩固机制及处理矛盾）[E:fact_bdcb3687eac8be91ae73e017]。该证据的 source_material_status 为 fetched、provenance_status 为 quote_bound，独立性未知，且未给出任务、数据集、模型、历史长度、检索预算与指标，因此无法提供这些条件下记忆压缩或粒度影响的证据，**不能作为压缩损失或长期一致性效果的证据**。该证据缺少 required_conditions 中的 task、dataset、model、compression_method、compression_ratio、context_budget、metric 等条件绑定。
- 现有材料中**没有任何一条**报告了「压缩方法 × 压缩比 × 上下文预算 × 指标」四元组齐备的长期一致性对比结果。用户事实抽取（如 COMEDY 的 Task 1/Task 2）是压缩流程的中间步骤，**不能等同于**对压缩后长期一致性的独立验证。

### 五、结论强度声明

两个候选命题（事实级 vs 会话级粒度效应、压缩比与一致性损失的权衡）在本批材料中均**未达到可判定状态**。已交付的 9 篇全文材料在实验设置、方法、结果区块上未通过审查，`materials_reviewed` 为 0，因此不能写「多篇独立研究已证明」，也不能写「同一设置下」。当前只能确认：测量压缩损失必须同时绑定任务、数据集、模型、压缩方法、压缩比、上下文预算与指标；缺少其中任一项，现有证据都不足以支持「压缩无损」或「压缩必然损失一致性」的结论。