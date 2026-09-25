// heyjev-fm: stdin JSON lines in, JSON lines out. One warm process, sequential.
// Requires macOS 26+ for Foundation Models; below that reports unavailable.
// Cancellation: no cancel message; parent enforces deadline/Stop by killing
// this process, discarding late output, and starting fresh for later requests.
// PCC (private_cloud) is intentionally DISABLED in this build: it requires the
// managed com.apple.developer.private-cloud-compute entitlement, which an
// ad-hoc/unsigned helper does not have. Models reports it unavailable and ask
// returns unavailable; on-device ships independently. Never fall back
// on-device -> PCC; selection is explicit by the user.
// Build note: PrivateCloudComputeLanguageModel exists only in the macOS 27 SDK.
// build.sh compiles the full file on a 27+ SDK; on a 26 SDK it retries with
// -D NO_PCC, which compiles out the PCC branches below.
import Foundation
import FoundationModels

let pccEnabled = false // flip only in an entitled, user-consented build

func out(_ dict: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: dict),
       let s = String(data: d, encoding: .utf8) { print(s); fflush(stdout) }
}

func onDeviceModel() -> (available: Bool, reason: String?, name: String) {
    if #available(macOS 26.0, *) {
        let a = SystemLanguageModel.default.availability
        switch a {
        case .available:
            var nm = "On this Mac"
            if #available(macOS 27.0, *) { nm = "On this Mac (\(SystemLanguageModel.default.variant.displayName))" }
            return (true, nil, nm)
        case .unavailable(.appleIntelligenceNotEnabled):
            return (false, "Apple Intelligence is off", "On this Mac")
        case .unavailable(.deviceNotEligible):
            return (false, "this Mac is not eligible", "On this Mac")
        case .unavailable(.modelNotReady):
            return (false, "model still downloading", "On this Mac")
        @unknown default:
            return (false, "not available", "On this Mac")
        }
    }
    return (false, "needs macOS 26", "On this Mac")
}

func pccModel() -> (omit: Bool, available: Bool, reason: String?) {
    if #available(macOS 27.0, *) {
        #if NO_PCC
        return (false, false, "cloud model; needs the Private Cloud Compute entitlement, not enabled in this build")
        #else
        if !pccEnabled {
            return (false, false, "cloud model; needs the Private Cloud Compute entitlement, not enabled in this build")
        }
        let a = PrivateCloudComputeLanguageModel().availability
        switch a {
        case .available: return (false, true, nil)
        case .unavailable(.deviceNotEligible): return (false, false, "this Mac is not eligible")
        case .unavailable(.systemNotReady): return (false, false, "Apple Intelligence is off or not ready")
        @unknown default: return (false, false, "not available")
        }
        #endif
    }
    return (true, false, nil)
}

func handleModels() {
    let od = onDeviceModel()
    var models: [[String: Any]] = [[
        "id": "on_device", "name": od.name, "remote": false,
        "available": od.available, "reason": od.reason as Any,
    ]]
    let pcc = pccModel()
    if !pcc.omit {
        models.append([
            "id": "private_cloud", "name": "Apple Private Cloud Compute",
            "remote": true, "available": pcc.available, "reason": pcc.reason as Any,
        ])
    }
    out(["op": "models", "models": models])
}

if #available(macOS 26.0, *) {
    while let line = readLine(strippingNewline: true) {
        guard let data = line.data(using: .utf8),
              let msg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let op = msg["op"] as? String else { continue }
        if op == "models" { handleModels(); continue }
        if op == "quit" { break }
        if op == "ask" {
            let qid = msg["id"] as? String ?? ""
            let model = msg["model"] as? String ?? "on_device"
            let instructions = msg["instructions"] as? String ?? ""
            let hist = msg["history"] as? [[String: String]] ?? []
            let prompt = msg["prompt"] as? String ?? ""
            let t0 = Date()
            if model == "private_cloud" {
                #if NO_PCC
                out(["op": "ask", "id": qid, "status": "unavailable", "text": "",
                     "error": "Private Cloud Compute not enabled in this build (needs entitlement)", "latency_ms": 0] as [String: Any])
                continue
                #else
                if !pccEnabled {
                    out(["op": "ask", "id": qid, "status": "unavailable", "text": "",
                         "error": "Private Cloud Compute not enabled in this build (needs entitlement)", "latency_ms": 0] as [String: Any])
                    continue
                }
                if #available(macOS 27.0, *) {
                    let a = PrivateCloudComputeLanguageModel().availability
                    if a != .available {
                        out(["op": "ask", "id": qid, "status": "unavailable", "text": "",
                             "error": "Private Cloud Compute not available", "latency_ms": 0] as [String: Any])
                        continue
                    }
                    let session = LanguageModelSession(model: PrivateCloudComputeLanguageModel()) {
                        if !instructions.isEmpty { Instructions(instructions) }
                    }
                    Task {
                        do {
                            for h in hist {
                                if h["role"] == "user" { _ = try? await session.respond(to: h["text"] ?? "") }
                            }
                            let resp = try await session.respond(to: prompt)
                            let ms = Int(Date().timeIntervalSince(t0) * 1000)
                            out(["op": "ask", "id": qid, "status": "finished",
                                 "text": String(describing: resp.content), "error": NSNull(),
                                 "latency_ms": ms] as [String: Any])
                        } catch {
                            let ms = Int(Date().timeIntervalSince(t0) * 1000)
                            out(["op": "ask", "id": qid, "status": "failed", "text": "",
                                 "error": String(describing: error), "latency_ms": ms] as [String: Any])
                        }
                    }
                    RunLoop.main.run(until: Date(timeIntervalSinceNow: 120))
                } else {
                    out(["op": "ask", "id": qid, "status": "unavailable", "text": "",
                         "error": "needs macOS 27", "latency_ms": 0] as [String: Any])
                }
                #endif
                continue
            }
            // on_device
            let davail = SystemLanguageModel.default.availability
            if davail != .available {
                out(["op": "ask", "id": qid, "status": "unavailable", "text": "",
                     "error": "on-device model not available", "latency_ms": 0] as [String: Any])
                continue
            }
            if !SystemLanguageModel.default.supportsLocale(Locale.current) {
                out(["op": "ask", "id": qid, "status": "unavailable", "text": "",
                     "error": "on-device model does not support this language", "latency_ms": 0] as [String: Any])
                continue
            }
            let maxTokens = msg["max_tokens"] as? Int ?? 256
            let session = LanguageModelSession {
                if !instructions.isEmpty { Instructions(instructions) }
            }
            let options = GenerationOptions(maximumResponseTokens: maxTokens)
            Task {
                do {
                    for h in hist {
                        if h["role"] == "user" { _ = try? await session.respond(to: h["text"] ?? "") }
                    }
                    let resp = try await session.respond(to: prompt, options: options)
                    let ms = Int(Date().timeIntervalSince(t0) * 1000)
                    out(["op": "ask", "id": qid, "status": "finished",
                         "text": String(describing: resp.content), "error": NSNull(),
                         "latency_ms": ms] as [String: Any])
                } catch {
                    let ms = Int(Date().timeIntervalSince(t0) * 1000)
                    out(["op": "ask", "id": qid, "status": "failed", "text": "",
                         "error": String(describing: error), "latency_ms": ms] as [String: Any])
                }
            }
            RunLoop.main.run(until: Date(timeIntervalSinceNow: 120))
            continue
        }
    }
} else {
    // Below macOS 26: answer models/unavailable for every line, then EOF exits.
    while let line = readLine(strippingNewline: true) {
        if let data = line.data(using: .utf8),
           let msg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let op = msg["op"] as? String {
            if op == "models" {
                out(["op": "models", "models": [[
                    "id": "on_device", "name": "On this Mac", "remote": false,
                    "available": false, "reason": "needs macOS 26",
                ]]])
            } else if op == "ask" {
                out(["op": "ask", "id": msg["id"] as? String ?? "", "status": "unavailable",
                     "text": "", "error": "needs macOS 26", "latency_ms": 0])
            } else if op == "quit" { break }
        }
    }
}
