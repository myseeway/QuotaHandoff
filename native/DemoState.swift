import Foundation

// Fictional data only. Demo mode never starts the backend or opens credentials.
func demoState() -> [String: Any] {
    let now = Date().timeIntervalSince1970
    return [
        "version": "1.10.0", "mode": "auto", "autoReset": false,
        "accounts": [
            ["key": "demo-personal", "alias": "Alex Chen", "email": "alex@example.com",
             "current": true, "rotationEnabled": true, "remaining": 68.0, "checkedAt": now,
             "windows": [["minutes": 10080.0, "remaining": 68.0, "resetsAt": now + 3*86400]],
             "cardCount": 2, "cardsComplete": true, "cards": [
                ["status": "available", "title": "额度重置卡", "expiresAt": now + 5*86400],
                ["status": "available", "title": "额度重置卡", "expiresAt": now + 12*86400]]],
            ["key": "demo-work", "alias": "Jamie Lin", "email": "jamie@example.com",
             "current": false, "rotationEnabled": true, "remaining": 24.0, "checkedAt": now,
             "windows": [["minutes": 10080.0, "remaining": 24.0, "resetsAt": now + 86400]],
             "cardCount": 1, "cardsComplete": true, "cards": [
                ["status": "available", "title": "额度重置卡", "expiresAt": now + 7*86400]]]
        ],
        "tasks": [["id": "demo-task", "title": "整理项目文档", "status": "active"]],
        "plan": ["action": "stay", "reason": "当前额度充足，继续使用"],
        "preflight": ["ready": true, "checks": [
            ["label": "当前登录身份已确认", "ready": true],
            ["label": "轮换账号额度已确认", "ready": true],
            ["label": "桌面任务状态已确认", "ready": true],
            ["label": "没有待检查的恢复记录", "ready": true]
        ]],
        "events": [], "pending": [], "desktopIssues": []
    ]
}
