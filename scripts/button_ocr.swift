import Foundation
import Vision

// 一行输入是一张 PNG 的 base64 JSON 字符串；逐帧返回完整文字，不猜测按钮状态。
while let line = readLine() {
    var texts: [String] = []
    do {
        let encoded = try JSONDecoder().decode(String.self, from: Data(line.utf8))
        if let png = Data(base64Encoded: encoded) {
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = ["zh-Hans", "en-US"]
            request.usesLanguageCorrection = false
            try VNImageRequestHandler(data: png).perform([request])
            // 上方主文案优先；调用方只判断第一行，不能被下方说明文字误触发。
            texts = (request.results ?? [])
                .sorted { $0.boundingBox.maxY > $1.boundingBox.maxY }
                .compactMap { $0.topCandidates(1).first?.string }
        }
    } catch {
        // 无法解码或识别时返回空列表，保持协议完整并让调用方拒绝本帧。
        texts = []
    }
    let result = try JSONEncoder().encode(texts)
    FileHandle.standardOutput.write(result)
    FileHandle.standardOutput.write(Data([10]))
}
