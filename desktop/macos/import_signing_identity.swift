// Setup-only importer: the temporary transport password travels over stdin,
// never in process arguments, environment variables, or logs.
import Foundation
import Security

func check(_ status: OSStatus) throws {
    guard status == errSecSuccess else {
        throw NSError(domain: NSOSStatusErrorDomain, code: Int(status), userInfo: nil)
    }
}

do {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard let input = try JSONSerialization.jsonObject(with: data) as? [String: String],
          let path = input["package"], let password = input["password"], let keychainPath = input["keychain"] else {
        throw NSError(domain: "chonkytasks.signing", code: 1)
    }
    var keychain: SecKeychain?
    try check(SecKeychainOpen(keychainPath, &keychain))
    var signer: SecTrustedApplication?
    try check(SecTrustedApplicationCreateFromPath("/usr/bin/codesign", &signer))
    var access: SecAccess?
    try check(SecAccessCreate("chonkytasks Local Development" as CFString, [signer!] as CFArray, &access))
    let options: [String: Any] = [
        kSecImportExportPassphrase as String: password,
        kSecImportExportKeychain as String: keychain!,
        kSecImportExportAccess as String: access!
    ]
    var items: CFArray?
    try check(SecPKCS12Import(try Data(contentsOf: URL(fileURLWithPath: path)) as CFData, options as CFDictionary, &items))
} catch {
    FileHandle.standardError.write(Data("Signing identity import failed: \(error.localizedDescription)\n".utf8))
    exit(1)
}
