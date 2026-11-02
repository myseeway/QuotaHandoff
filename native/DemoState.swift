import Foundation

// Fictional data only. Demo mode never starts the backend or opens credentials.
func demoState() -> [String: Any] {
    let now = Date().timeIntervalSince1970
    let day: TimeInterval = 86400
    // Explicit intermediate types keep this fixture inexpensive for older Swift compilers.
    let personalCards: [[String: Any]] = [
        ["status": "available", "title": "额度重置卡", "expiresAt": now + 5 * day],
        ["status": "available", "title": "额度重置卡", "expiresAt": now + 12 * day]
    ]
    let workCards: [[String: Any]] = [
        ["status": "available", "title": "额度重置卡", "expiresAt": now + 7 * day]
    ]
    let personalWindows: [[String: Any]] = [
        ["minutes": 10080.0, "remaining": 68.0, "resetsAt": now + 3 * day]
    ]
    let workWindows: [[String: Any]] = [
        ["minutes": 10080.0, "remaining": 24.0, "resetsAt": now + day]
    ]
    let personal: [String: Any] = [
        "key": "demo-personal", "alias": "Alex Chen", "email": "alex@example.com",
        "current": true, "rotationEnabled": true, "remaining": 68.0, "checkedAt": now,
        "windows": personalWindows, "cardCount": 2, "cardsComplete": true, "cards": personalCards
    ]
    let work: [String: Any] = [
        "key": "demo-work", "alias": "Jamie Lin", "email": "jamie@example.com",
        "current": false, "rotationEnabled": true, "remaining": 24.0, "checkedAt": now,
        "windows": workWindows, "cardCount": 1, "cardsComplete": true, "cards": workCards
    ]
    let checks: [[String: Any]] = [
        ["label": "当前登录身份已确认", "ready": true],
        ["label": "轮换账号额度已确认", "ready": true],
        ["label": "桌面任务状态已确认", "ready": true],
        ["label": "没有待检查的恢复记录", "ready": true]
    ]
    let preflight: [String: Any] = ["ready": true, "checks": checks]
    let tasks: [[String: Any]] = [["id": "demo-task", "title": "整理项目文档", "status": "active"]]
    return [
        "version": "1.10.1", "mode": "auto", "autoReset": false,
        "accounts": [personal, work], "tasks": tasks,
        "plan": ["action": "stay", "reason": "当前额度充足，继续使用"],
        "preflight": preflight,
        "events": [], "pending": [], "desktopIssues": []
    ]
}
