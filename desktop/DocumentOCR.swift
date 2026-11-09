import Foundation
import AppKit
import PDFKit
import Vision

func extract(_ path: String) throws -> String {
    guard let document = PDFDocument(url: URL(fileURLWithPath: path)), !document.isEncrypted, document.pageCount <= 20 else {
        throw NSError(domain: "OCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "OCR supports unencrypted PDFs up to 20 pages"])
    }
    var pages: [String] = []
    var total = 0
    for index in 0..<document.pageCount {
        try autoreleasepool {
            guard let page = document.page(at: index) else { return }
            if let text = page.string, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                pages.append("[Страница \(index + 1)]\n" + text)
                total += text.utf8.count
                return
            }
            let bounds = page.bounds(for: .mediaBox)
            let scale = min(2.5, 2500 / max(bounds.width, bounds.height))
            let thumbnail = page.thumbnail(of: NSSize(width: max(1, bounds.width * scale), height: max(1, bounds.height * scale)), for: .mediaBox)
            var rectangle = NSRect(origin: .zero, size: thumbnail.size)
            guard let image = thumbnail.cgImage(forProposedRect: &rectangle, context: nil, hints: nil) else { return }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = true
            let supported = try request.supportedRecognitionLanguages()
            let languages = ["ru-RU", "en-US"].filter { supported.contains($0) }
            if !languages.isEmpty { request.recognitionLanguages = languages }
            try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
            let observations = (request.results ?? []).sorted { a, b in
                if abs(a.boundingBox.midY - b.boundingBox.midY) > 0.01 { return a.boundingBox.midY > b.boundingBox.midY }
                return a.boundingBox.minX < b.boundingBox.minX
            }
            let text = observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
            total += text.utf8.count
            pages.append("[Страница \(index + 1), OCR]\n" + text)
        }
        if total > 480 * 1024 { throw NSError(domain: "OCR", code: 2, userInfo: [NSLocalizedDescriptionKey: "OCR text exceeds 480 KiB"]) }
    }
    if total == 0 { throw NSError(domain: "OCR", code: 3, userInfo: [NSLocalizedDescriptionKey: "No text recognized"]) }
    return pages.joined(separator: "\n\n")
}

do {
    guard CommandLine.arguments.count == 2 else { exit(2) }
    let text = try extract(CommandLine.arguments[1])
    let data = try JSONSerialization.data(withJSONObject: ["text": text, "engine": "Apple Vision"], options: [])
    FileHandle.standardOutput.write(data)
} catch {
    let failure = error as NSError
    FileHandle.standardError.write(Data("OCR error: \(failure.domain) \(failure.code)\n".utf8))
    let data = try! JSONSerialization.data(withJSONObject: ["error": "Local OCR failed"], options: [])
    FileHandle.standardOutput.write(data)
    exit(1)
}
