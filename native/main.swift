import AppKit
import SwiftUI
import ServiceManagement
import Darwin

#if DEMO
let demoMode = true
#else
let demoMode = CommandLine.arguments.contains("--demo")
#endif

if !demoMode && CommandLine.arguments.contains("--quit-desktop") {
    let targets = NSRunningApplication.runningApplications(withBundleIdentifier: "com.openai.codex")
    let accepted = targets.map { $0.terminate() }.allSatisfy { $0 }
    exit(accepted ? 0 : 1)
}

final class Model: ObservableObject {
    @Published var state: [String: Any] = [:]
    @Published var launchAtLogin = !demoMode && SMAppService.mainApp.status == .enabled
    @Published var appError: String?
    @Published var showSettings = false
    @Published var expandedAccounts: Set<String> = []
    var process: Process?
    var input: FileHandle?
    var onUpdate: (() -> Void)?
    private var awaitingManualRequest: String?
    private var buffer = Data()
    var accounts: [[String: Any]] { state["accounts"] as? [[String: Any]] ?? [] }
    var tasks: [[String: Any]] { state["tasks"] as? [[String: Any]] ?? [] }
    var mode: String { state["mode"] as? String ?? "observe" }
    var current: [String: Any]? { accounts.first { $0["current"] as? Bool == true } }
    var remaining: Double? { current?["remaining"] as? Double }
    var busy: Bool { state["refreshing"] as? Bool == true }
    var visibleManualResult: [String: Any]? {
        guard let result = state["manualSwitchResult"] as? [String: Any] else { return nil }
        let status = result["status"] as? String
        if status == "complete" || status == "cancelled" {
            guard let at = result["at"] as? Double, Date().timeIntervalSince1970 - at < 30 else { return nil }
        }
        return result
    }
    var modeTitle: String { mode == "auto" ? "自动调度" : mode == "paused" ? "暂停监控" : "仅观察" }
    var nextModeTitle: String { mode == "auto" ? "暂停监控" : mode == "paused" ? "仅观察" : "自动调度" }
    var modeDescription: String {
        mode == "auto" ? "额度不超过 5% 时调度账号并恢复符合条件的任务。" :
        mode == "paused" ? "已暂停定时查询与自动调度，可手动刷新额度。" : "定时更新额度，不自动切换账号或运行任务。"
    }

    func start() {
        if demoMode {
            state = demoState()
            onUpdate?()
            return
        }
        guard let resource = Bundle.main.resourceURL, let exe = Bundle.main.executableURL else { return }
        let p = Process(), incoming = Pipe(), outgoing = Pipe()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [resource.appendingPathComponent("backend/service.py").path, exe.path]
        p.standardInput = incoming
        p.standardOutput = outgoing
        p.standardError = FileHandle.nullDevice
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = env
        input = incoming.fileHandleForWriting
        outgoing.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                guard let self else { return }
                self.buffer.append(data)
                while let end = self.buffer.firstIndex(of: 10) {
                    let line = self.buffer.prefix(upTo: end)
                    self.buffer.removeSubrange(...end)
                    if let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                        self.state = value
                        self.onUpdate?()
                        self.showManualResultIfNeeded()
                    }
                }
            }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.appError = "监控进程已退出，请重新打开小组件" }
        }
        do { try p.run(); process = p }
        catch { appError = "监控进程启动失败：\(error.localizedDescription)" }
    }
    func send(_ command: String, _ extra: [String: Any] = [:]) {
        if demoMode {
            if command == "cycle_mode" {
                state["mode"] = mode == "observe" ? "auto" : mode == "auto" ? "paused" : "observe"
            } else if command == "auto_reset" {
                state["autoReset"] = extra["value"]
            }
            onUpdate?()
            return
        }
        var msg = extra
        msg["command"] = command
        guard var data = try? JSONSerialization.data(withJSONObject: msg) else { return }
        data.append(10)
        do {
            guard let input else { appError = "监控进程尚未连接，请重新打开小组件"; return }
            try input.write(contentsOf: data)
        } catch { appError = "操作未发送，请重新打开小组件后重试" }
    }
    func toggleReset(_ enabled: Bool) {
        if demoMode { send("auto_reset", ["value": enabled]); return }
        if enabled {
            let a = NSAlert()
            a.messageText = "允许自动使用已有重置卡？"
            a.informativeText = "自动调度时，所有参与轮换的账号额度均不超过 5%，将使用最早到期的有效重置卡。每次兑换消耗一张卡。"
            a.addButton(withTitle: "允许")
            a.addButton(withTitle: "取消")
            guard a.runModal() == .alertFirstButtonReturn else { return }
        }
        send("auto_reset", ["value": enabled])
    }
    func importAccount() {
        guard !demoMode else { return }
        let panel = NSOpenPanel()
        panel.title = "选择另一个账号的 auth.json"
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex")
        panel.showsHiddenFiles = true
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { send("import", ["path": url.path]) }
    }
    func switchAccount(_ account: [String: Any]) {
        guard !demoMode else { return }
        guard let key = account["key"] as? String else { return }
        let alert = NSAlert()
        alert.messageText = "切换到 \(account["alias"] as? String ?? "此账号")？"
        alert.informativeText = "停止当前运行的聊天，切换登录并重新打开 ChatGPT。手动切换不检查剩余额度、不使用重置卡、不自动恢复任务，完成后进入“仅观察”模式。"
        alert.addButton(withTitle: "切换账号")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            let request = UUID().uuidString
            awaitingManualRequest = request
            send("switch_account", ["key": key, "requestId": request])
        }
    }
    func showManualResultIfNeeded() {
        guard let request = awaitingManualRequest,
              let result = state["manualSwitchResult"] as? [String: Any],
              result["id"] as? String == request,
              let status = result["status"] as? String, status != "pending" else { return }
        awaitingManualRequest = nil
        guard status == "failed" else { return }
        let message = result["message"] as? String ?? "请打开小组件检查状态"
        // One alert for the user's current request; never replay old alerts on launch.
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.messageText = "账号切换未完成"
            alert.informativeText = message
            alert.addButton(withTitle: "知道了")
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }
    func setRotation(_ enabled: Bool, account: [String: Any]) {
        guard !demoMode else { return }
        guard let key = account["key"] as? String else { return }
        if enabled {
            let alert = NSAlert()
            alert.messageText = "将 \(account["alias"] as? String ?? "此账号") 加入自动轮换？"
            alert.informativeText = "额度不足时，工具可以切换到此账号，并按已开启的用卡规则恢复额度。"
            alert.addButton(withTitle: "加入轮换")
            alert.addButton(withTitle: "取消")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        send("rotation", ["key": key, "value": enabled])
    }
    func openKeychainAccess() {
        guard !demoMode else { return }
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.keychainaccess") else {
            appError = "请在系统中打开“钥匙串访问”，解锁后点击刷新"; return
        }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
    func setLogin(_ enabled: Bool) {
        guard !demoMode else { return }
        do {
            if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            launchAtLogin = SMAppService.mainApp.status == .enabled
        } catch { appError = "登录启动设置失败，请在系统设置的登录项中检查" }
    }
    func stop() {
        guard !demoMode else { return }
        send("quit")
        try? input?.close()
    }
}

func dateText(_ value: Any?, timeOnly: Bool = false, includeYear: Bool = false) -> String {
    guard let seconds = value as? Double else { return "时间未知" }
    let f = DateFormatter()
    f.dateFormat = timeOnly ? "HH:mm:ss" : includeYear ? "yyyy/MM/dd HH:mm" : "MM/dd HH:mm"
    f.timeZone = TimeZone.current
    return f.string(from: Date(timeIntervalSince1970: seconds))
}
func windowName(_ m: Double) -> String {
    if m == 10080 { return "周额度" }
    if m == 300 { return "5 小时" }
    if m >= 1440 { return "\(Int(m/1440)) 天" }
    return "\(Int(m)) 分钟"
}
let accent = Color(red: 0.10, green: 0.58, blue: 0.48)

struct AccountCard: View {
    let data: [String: Any]
    @Binding var expanded: Bool
    let canSwitch: Bool
    let requestSwitch: () -> Void
    let setRotation: (Bool) -> Void
    var cards: [[String: Any]] { data["cards"] as? [[String: Any]] ?? [] }
    func cardStatus(_ card: [String: Any]) -> String {
        let status = card["status"] as? String
        if status == "redeemed" || status == "consumed" { return "已使用" }
        if status == "expired" || (card["expiresAt"] as? Double).map({ $0 <= Date().timeIntervalSince1970 }) == true { return "已到期" }
        return status == "available" ? "可用" : "状态待确认"
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(data["alias"] as? String ?? "账号").font(.system(size: 14, weight: .semibold)).lineLimit(2)
                    if let email = data["email"] as? String, !email.isEmpty, email != data["alias"] as? String {
                        Text(email).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle).help(email)
                    }
                }
                if data["current"] as? Bool == true {
                    Text("当前使用").font(.system(size: 10, weight: .medium)).foregroundStyle(accent)
                        .padding(.horizontal, 7).padding(.vertical, 3).background(accent.opacity(0.12), in: Capsule())
                }
                Spacer()
                if let n = data["remaining"] as? Double {
                    Text("\(Int(n))%").font(.system(size: 25, weight: .semibold, design: .rounded))
                        .foregroundStyle(n <= 5 ? Color.orange : accent)
                } else { Text("—").font(.title2).foregroundStyle(.secondary) }
            }
            if let error = data["error"] as? String {
                Label(error, systemImage: "exclamationmark.circle").font(.caption).foregroundStyle(.orange)
            } else {
                ForEach(Array((data["windows"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, w in
                    let n = w["remaining"] as? Double ?? 0
                    VStack(spacing: 4) {
                        ProgressView(value: n, total: 100).tint(n <= 5 ? .orange : accent)
                        HStack {
                            Text("\(windowName(w["minutes"] as? Double ?? 0)) · 剩余 \(Int(n))%")
                            Spacer()
                            Text("\(dateText(w["resetsAt"])) 重置")
                        }.font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                }
                let count = data["cardCount"] as? Int
                Button {
                    withAnimation(.easeInOut(duration: 0.18)) { expanded.toggle() }
                } label: {
                    HStack(spacing: 5) {
                        Label(count == nil ? "重置卡待确认" : "\(count!) 张可用重置卡", systemImage: "ticket")
                        Spacer(minLength: 2)
                        let dates = cards.filter { cardStatus($0) == "可用" }.compactMap { $0["expiresAt"] as? Double }.sorted()
                        if let first = dates.first { Text("最早 \(dateText(first)) 到期").font(.system(size: 10)) }
                        Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.system(size: 9, weight: .semibold))
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
                    .help(expanded ? "收起重置卡明细" : "展开所有重置卡及到期日期")
                    .accessibilityLabel("\(data["alias"] as? String ?? "账号")的重置卡")
                    .accessibilityValue(expanded ? "已展开" : "已收起")
                if expanded {
                    VStack(alignment: .leading, spacing: 9) {
                        if cards.isEmpty {
                            Text(data["cardsComplete"] as? Bool == true ? "暂无重置卡" : "尚未取得重置卡明细，请刷新后重试。")
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                        ForEach(Array(cards.enumerated()), id: \.offset) { index, card in
                            HStack(alignment: .top, spacing: 8) {
                                Image(systemName: "ticket").foregroundStyle(accent).padding(.top, 2)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text((card["title"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "重置卡 \(index + 1)")
                                        .font(.system(size: 11, weight: .medium))
                                    Text(card["expiresAt"] is Double ? "\(dateText(card["expiresAt"], includeYear: true)) 到期" : "未设置到期时间")
                                        .font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text(cardStatus(card)).font(.system(size: 10))
                                    .foregroundStyle(cardStatus(card) == "可用" ? accent : .secondary)
                            }
                        }
                        if !cards.isEmpty && data["cardsComplete"] as? Bool != true {
                            Text("部分明细尚未返回；自动调度会等待完整信息。")
                                .font(.system(size: 10)).foregroundStyle(.orange)
                        }
                    }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                        .background(accent.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
                }
                if let checked = data["checkedAt"] as? Double {
                    Text("更新于 \(dateText(checked, timeOnly: true))").font(.system(size: 9)).foregroundStyle(.tertiary)
                }
            }
            HStack {
                Toggle("自动轮换", isOn: Binding(get: { data["rotationEnabled"] as? Bool ?? true }, set: setRotation))
                    .toggleStyle(.switch).controlSize(.mini).font(.system(size: 10)).fixedSize()
                    .disabled(!canSwitch)
                Spacer()
                if data["current"] as? Bool != true {
                    Button(action: requestSwitch) {
                        Label("切换到此账号", systemImage: "arrow.left.arrow.right")
                    }.buttonStyle(.borderless).font(.system(size: 11)).foregroundStyle(accent)
                        .disabled(!canSwitch)
                } else if data["rotationEnabled"] as? Bool == false {
                    Text("仅监控").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }
        }.padding(14).background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 13))
    }
}

struct Panel: View {
    @ObservedObject var model: Model
    let size: CGSize
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "chart.pie.fill").font(.system(size: 24)).foregroundStyle(accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("QuotaHandoff").font(.system(size: 17, weight: .semibold))
                    Text(demoMode ? "演示数据 · 额度接力" : "\(model.accounts.count) 个账号，一处掌握").font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                Button { model.send("cycle_mode") } label: {
                    HStack(spacing: 5) {
                        Image(systemName: model.mode == "auto" ? "arrow.triangle.2.circlepath" : model.mode == "paused" ? "pause.circle" : "eye")
                        Text(model.modeTitle)
                    }.font(.system(size: 11, weight: .medium)).lineLimit(1)
                        .padding(.horizontal, 10).padding(.vertical, 7).frame(width: 104)
                        .background((model.mode == "paused" ? Color.orange : accent).opacity(0.12), in: Capsule())
                        .foregroundStyle(model.mode == "paused" ? Color.orange : accent)
                }.buttonStyle(.plain).help("点击切换为\(model.nextModeTitle)；循环顺序：仅观察 → 自动调度 → 暂停监控")
                    .accessibilityLabel("监控模式：\(model.modeTitle)")
                    .accessibilityHint("点击切换为\(model.nextModeTitle)")
            }
            Text(model.modeDescription).font(.system(size: 10)).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading).frame(height: 28, alignment: .topLeading)
            ScrollView(.vertical) {
                VStack(alignment: .leading, spacing: 12) {
                    if let result = model.visibleManualResult {
                        let status = result["status"] as? String ?? "pending"
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Label(status == "pending" ? "正在手动切换" : status == "complete" ? "手动切换已完成" : status == "failed" ? "手动切换未完成" : "手动切换已取消",
                                      systemImage: status == "failed" ? "exclamationmark.triangle" : "arrow.left.arrow.right")
                                    .font(.system(size: 12, weight: .semibold))
                                Spacer()
                                if status == "pending" {
                                    ProgressView().controlSize(.small)
                                    Button("取消") { model.send("cancel_switch") }.font(.caption)
                                }
                            }
                            Text(result["message"] as? String ?? "正在处理").font(.system(size: 11)).foregroundStyle(.secondary)
                        }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                            .background((status == "failed" ? Color.orange : accent).opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
                    }
                    if model.accounts.isEmpty {
                        VStack(spacing: 10) { ProgressView(); Text("正在读取账号与额度…").font(.caption).foregroundStyle(.secondary) }
                            .frame(maxWidth: .infinity).padding(24)
                    }
                    ForEach(model.accounts.compactMap { $0["key"] as? String }, id: \.self) { key in
                        if let account = model.accounts.first(where: { $0["key"] as? String == key }) {
                            AccountCard(data: account, expanded: Binding(get: { model.expandedAccounts.contains(key) }, set: { value in
                                if value { model.expandedAccounts.insert(key) } else { model.expandedAccounts.remove(key) }
                            }), canSwitch: model.state["manualSwitchPending"] as? Bool != true,
                                requestSwitch: { model.switchAccount(account) },
                                setRotation: { model.setRotation($0, account: account) })
                        }
                    }
                    let plan = model.state["plan"] as? [String: Any] ?? [:]
                    VStack(alignment: .leading, spacing: 6) {
                        Label(model.mode == "auto" ? "调度状态" : "下一步预览", systemImage: "arrow.triangle.branch")
                            .font(.system(size: 12, weight: .semibold))
                        Text(model.mode == "auto" ? plan["reason"] as? String ?? "等待数据" : "额度不超过 5% 时优先选择可用账号；全部不足时按重置卡到期顺序处理。")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                        if model.mode == "auto", let wake = plan["wakeAt"] as? Double { Text("预计 \(dateText(wake)) 后复查").font(.caption) }
                    }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(accent.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                    if let operation = model.state["operation"] as? [String: Any] {
                        VStack(alignment: .leading, spacing: 6) {
                            Label(operation["title"] as? String ?? "正在处理", systemImage: "arrow.triangle.2.circlepath")
                                .font(.system(size: 12, weight: .semibold))
                            let total = operation["total"] as? Int ?? 0
                            let completed = operation["completed"] as? Int ?? 0
                            let skipped = operation["skipped"] as? Int ?? 0
                            if total > 0 {
                                ProgressView(value: Double(completed + skipped), total: Double(total)).tint(accent)
                                Text("\(total) 个原任务 · \(completed) 个已恢复或已继续 · \(skipped) 个已跳过")
                                    .font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                        }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                            .background(accent.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                    }
                    if let error = model.appError ?? model.state["problem"] as? String {
                        Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                    }
                    if model.state["credentialNeedsAttention"] as? Bool == true {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("钥匙串需要确认").font(.system(size: 12, weight: .semibold))
                            Text("解锁后会自动复查，也可点击刷新立即重试。账号切换尚未执行。")
                                .font(.system(size: 10)).foregroundStyle(.secondary)
                            HStack {
                                Button("打开钥匙串访问", action: model.openKeychainAccess)
                                Button("重新检查") { model.send("refresh") }
                            }.font(.system(size: 11))
                        }
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("桌面任务").font(.system(size: 12, weight: .semibold))
                            Spacer()
                            Text("\(model.tasks.filter { $0["status"] as? String == "active" }.count) 运行中")
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                        if let error = model.state["desktopError"] as? String { Text(error).font(.caption).foregroundStyle(.secondary) }
                        let issues = model.state["desktopIssues"] as? [[String: Any]] ?? []
                        if !issues.isEmpty {
                            DisclosureGroup("查看待确认记录（\(issues.count)）") {
                                ForEach(Array(issues.enumerated()), id: \.offset) { _, issue in
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(issue["title"] as? String ?? "未识别记录").font(.system(size: 11, weight: .medium))
                                        Text(issue["reason"] as? String ?? "等待确认").font(.system(size: 10))
                                        Text(issue["id"] as? String ?? "").font(.system(size: 9, design: .monospaced)).textSelection(.enabled)
                                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 3)
                                }
                            }.font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                        ForEach(Array(model.tasks.prefix(5).enumerated()), id: \.offset) { _, task in
                            HStack(spacing: 7) {
                                Circle().fill(task["status"] as? String == "active" ? accent : .gray.opacity(0.5)).frame(width: 6, height: 6)
                                Text(task["title"] as? String ?? "任务").font(.system(size: 11)).lineLimit(1)
                                Spacer()
                                Text(task["waiting"] as? Bool == true ? "等待操作" : task["goal"] as? String == "paused" ? "已暂停" : task["status"] as? String == "active" ? "运行" : "空闲")
                                    .font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                        }
                    }
                    let pending = model.state["pending"] as? [[String: Any]] ?? []
                    if model.state["canRetryFailedQuit"] as? Bool == true {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("上次退出未完成").font(.system(size: 12, weight: .semibold))
                            Text("如有未关闭的退出提示，请先取消。重新核验任务状态后，可继续自动调度。")
                                .font(.system(size: 10)).foregroundStyle(.secondary)
                            Button("重新核验并调度") { model.send("retry_failed_quit") }.font(.system(size: 11))
                        }
                    }
                    if !pending.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("待恢复任务").font(.system(size: 12, weight: .semibold))
                            ForEach(Array(pending.enumerated()), id: \.offset) { _, task in
                                HStack {
                                    Text(task["title"] as? String ?? "任务").font(.caption).lineLimit(1)
                                    Spacer()
                                    let phase = task["phase"] as? String ?? "saved"
                                    if phase == "saved" {
                                        Button("不恢复") { model.send("exclude", ["id": task["id"] ?? ""]) }.font(.caption)
                                    } else {
                                        Text(phase == "done" ? "已继续" : phase == "excluded" ? "已跳过" : "核对发送结果")
                                            .font(.system(size: 10)).foregroundStyle(.secondary)
                                    }
                                }
                            }
                            Button("取消本次恢复计划") { model.send("clear_pending") }.font(.caption)
                        }
                    }
                    if model.showSettings {
                        Divider()
                        let preflight = model.state["preflight"] as? [String: Any] ?? [:]
                        VStack(alignment: .leading, spacing: 6) {
                            Text("自动调度准备").font(.system(size: 12, weight: .semibold))
                            ForEach(Array((preflight["checks"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, check in
                                HStack(spacing: 6) {
                                    Image(systemName: check["ready"] as? Bool == true ? "checkmark.circle.fill" : "clock")
                                        .foregroundStyle(check["ready"] as? Bool == true ? accent : .secondary)
                                    Text(check["label"] as? String ?? "正在检查")
                                }.font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                        }.padding(.bottom, 4)
                        Toggle("允许自动使用已有重置卡", isOn: Binding(get: { model.state["autoReset"] as? Bool ?? false }, set: model.toggleReset))
                        Text("所有轮换账号额度均不超过 5% 时，使用最早到期的有效卡；没有可用卡则等待额度刷新。")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                        Toggle("登录时启动", isOn: Binding(get: { model.launchAtLogin }, set: model.setLogin))
                        Button("载入另一个登录文件…", action: model.importAccount)
                        Text("自动调度时，当前账号通常每 30 秒查询，接近阈值且有任务运行时每 10 秒查询。备用账号与仅观察模式每分钟查询；查询失败后延长重试间隔。")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                        Text("版本 1.10.0 · 自动调度仍处于实验阶段。所有时间采用系统时区。")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                        let events = model.state["events"] as? [[String: Any]] ?? []
                        if let completed = model.state["lastCompleted"] as? [String: Any] {
                            let captured = completed["captured"] as? Int ?? 0
                            Text(captured == 0 ? "上次调度：没有待恢复任务" : "上次调度：记录 \(captured) 项，确认续跑 \(completed["continued"] as? Int ?? 0) 项，跳过 \(completed["skipped"] as? Int ?? 0) 项")
                                .font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                        ForEach(Array(events.prefix(4).enumerated()), id: \.offset) { _, event in
                            Text("\(dateText(event["at"], timeOnly: true))  \(event["message"] as? String ?? "")")
                                .font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            HStack(spacing: 12) {
                Button { model.send("refresh") } label: {
                    Image(systemName: "arrow.clockwise")
                }.help("刷新额度；连续点击会合并，重试等待期间不会重复请求")
                if model.busy { ProgressView().controlSize(.small) }
                if !model.busy && model.state["refreshQueued"] as? Bool == true {
                    Text("等待刷新").font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer()
                Button { model.showSettings.toggle() } label: { Image(systemName: "slider.horizontal.3") }.help("设置")
                Button { NSApp.terminate(nil) } label: { Image(systemName: "power") }.help("退出小组件")
            }.buttonStyle(.borderless)
        }.padding(18).frame(width: size.width, height: size.height, alignment: .topLeading)
    }
}

final class Delegate: NSObject, NSApplicationDelegate {
    let model = Model()
    var item: NSStatusItem!
    var popover = NSPopover()
    var panelController: NSHostingController<Panel>!
    var terminationSignal: DispatchSourceSignal?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        signal(SIGTERM, SIG_IGN)
        terminationSignal = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        terminationSignal?.setEventHandler { NSApp.terminate(nil) }
        terminationSignal?.resume()
        // Keep the popover anchor stable when the title changes between a percentage and pause.
        item = NSStatusBar.system.statusItem(withLength: 72)
        item.button?.image = NSImage(systemSymbolName: "chart.pie.fill", accessibilityDescription: "QuotaHandoff")
        item.button?.imagePosition = .imageLeading
        item.button?.title = " …"
        item.button?.target = self
        item.button?.action = #selector(toggle)
        popover.behavior = .transient
        panelController = NSHostingController(rootView: Panel(model: model, size: panelSize()))
        // Content updates must not resize/recenter the hosting view inside a visible popover.
        panelController.sizingOptions = []
        popover.contentViewController = panelController
        updatePanelSize()
        model.onUpdate = { [weak self] in
            guard let self else { return }
            self.item.button?.title = self.model.mode == "paused" ? " Ⅱ" : self.model.remaining.map { " \(Int($0))%" } ?? " —"
            self.item.button?.toolTip = "QuotaHandoff · 额度接力"
        }
        model.start()
        if demoMode || CommandLine.arguments.contains("--show") { DispatchQueue.main.asyncAfter(deadline: .now()+0.5) { self.toggle() } }
    }
    func panelSize() -> NSSize {
        let screen = item.button?.window?.screen ?? NSScreen.main
        return NSSize(width: 398, height: max(320, min(680, (screen?.visibleFrame.height ?? 740) - 24)))
    }
    func updatePanelSize() {
        let size = panelSize()
        if panelController.rootView.size != size {
            panelController.rootView = Panel(model: model, size: size)
        }
        panelController.preferredContentSize = size
        // NSPopover owns the content origin/insets after presentation. Resetting it to
        // zero on a later open shifts the entire panel under the popover's left border.
        panelController.view.setFrameSize(size)
        popover.contentSize = size
    }
    @objc func toggle() {
        guard let button = item.button else { return }
        if popover.isShown { popover.performClose(nil) }
        else {
            updatePanelSize()
            NSApp.activate(ignoringOtherApps: true)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !popover.isShown { toggle() }
        return true
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        model.stop()
        return .terminateNow
    }
}
let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.run()
