using System.Security.Cryptography;
using System.Text;

namespace Wax.Hashing;

/// <summary>
/// A key derived from a name, as WORKLOADS.md defines it: the first 16 bytes
/// of the name's SHA-256, marked as a version 4 RFC 4122 UUID. Python's
/// <c>uuid.UUID(bytes=sha256(name)[:16], version=4)</c> gives the same key.
/// </summary>
public static class DerivedUuid
{
    public static Guid From(string name)
    {
        Span<byte> digest = stackalloc byte[SHA256.HashSizeInBytes];
        SHA256.HashData(Encoding.UTF8.GetBytes(name), digest);
        var bytes = digest[..16];
        bytes[6] = (byte)((bytes[6] & 0x0F) | 0x40);
        bytes[8] = (byte)((bytes[8] & 0x3F) | 0x80);
        return new Guid(bytes, bigEndian: true);
    }
}
