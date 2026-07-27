# L6-AUTH-R2 实时权限撤销与状态完整性收敛任务书

> 发布/执行日期：2026-07-27
> 重新打开基线：`95ec7a6d743ac37d91586233257589e6ab8c5e4a`
> 交付提交：`e8e0973e527abc238466b0b0d0734ca4c3a35083`
> 轨道：L5-SBX / L6-SBX（offline、fixed-synthetic、unit/in-memory）

## 重新打开原因

`ACC-20260727-056` 通过后追加的撤权探针证明：L6 记录操作仍可消费已经验证过但不再代表当前授权的投影；当前 rule bundle 被撤销后，Assembler 仍可能成功。进一步的同进程对抗复核还发现，实例方法遮蔽、外部 authorizer/verifier 重入、可执行容器或锁替换、隐藏状态和标量子类可能造成“校验前后不是同一能力/状态”的漂移。

因此暂停 `DEC-20260727-052` 的 L7-SBX 发布授权，追加式重新打开 L5-SBX/L6-SBX 组合验收。旧验收保留为历史事实，但不得继续作为当前 L7 授权证据。

## 必须关闭的实现边界

1. 四个记录边界（Assembler、Verifier、Store、Pipeline）必须消费由 exact `SandboxRecheckCoordinator` 实时产生的 active authorized projection；不得消费调用方复制的 snapshot。
2. 当前 rule-bundle authorizer 必须是每次记录操作的线性化授权点；撤权后的新操作全部 fail-closed，已经越过授权点的重叠操作按既定线性化语义完成。
3. authority projection 必须在 external signature verifier 和 authorizer 回调前后复核 coordinator、review coordinator、review store、锁、内部容器及完整状态；回调重入不能偷换当前引用或持久状态。
4. sealed capture 不得在校验前读取可执行 revision 容器；锁必须是 exact RLock，持久容器必须是 exact built-in list，元素与模型图必须执行 exact-type 检查。
5. hidden/extra/private 属性、错误/可执行容器、标量子类以及 canonical round-trip 可掩盖的类型漂移必须 fail-closed。
6. Review create/eligibility、Recheck completion/projection、Record assembler/pipeline 的 public scope 必须要求 exact `str`，不得让 `str` 子类在授权回调中注入行为。
7. Assembler、Verifier、Store 分别获取 fresh active projection；Pipeline 只获取一次并贯穿 assemble → verify → store，避免单次操作内部混用权限版本。

## 有限威胁模型

本任务防御的是 L5/L6 公共能力路径内、可由注入协作者或同进程对象图触发的撤权竞态、重入、实例遮蔽和状态漂移。Python 对象不是进程隔离边界；任意调用未公开私有方法、修改类级定义、绕过锁直接写内存或控制解释器的攻击者不在本次 SBX 合同内。产品 Runtime、HTTP、DB、部署、真实数据和临床工作流也不在范围内。

## 验收门禁

- 必须先证明“撤权后至少一个记录边界仍成功”的 RED，再使四个边界全部 GREEN。
- nested override、回调重入、锁/容器替换、hidden/scalar drift 和 hostile public scope 探针必须 fail-closed。
- L5/L6 全组合、全量非 integration、Ruff、`mypy app scripts`、lock 与 diff 门禁通过。
- 全仓 mypy 债务单独量化，不得把测试注解债务混写成生产源码通过或失败。
- 独立只读架构复核 P0/P1/P2/P3 全为 0 后，才能重新授权下一项 L7-SBX bounded task。
