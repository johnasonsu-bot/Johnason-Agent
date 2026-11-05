/* MIT; local Johnason adapter. Secrets travel through pipes, never argv/env/files. */
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>

#define LIMIT (4 * 1024 * 1024)
static void wipe(unsigned char *data, size_t size) {
  volatile unsigned char *p = data;
  while (size--) *p++ = 0;
}
static int status_code(OSStatus status) {
  if (status == errSecSuccess) return 0;
  if (status == errSecItemNotFound) return 44;
  if (status == errSecInteractionNotAllowed || status == errSecAuthFailed) return 73;
  return 70;
}
static int own_store(const char *account) {
  char path[256];
  snprintf(path, sizeof(path), "/private/tmp/johnason-dsh-subscriptions-%u-%s.lock", (unsigned)getuid(), account);
  int fd = open(path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0) return 70;
  struct stat info;
  if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != getuid()
      || (info.st_mode & 0777) != 0600 || info.st_nlink != 1 || info.st_size != 0) {
    close(fd); return 70;
  }
  if (flock(fd, LOCK_EX | LOCK_NB)) {
    int code = errno == EWOULDBLOCK ? 75 : 70;
    close(fd); return code;
  }
  if (fputs("locked\n", stdout) == EOF || fflush(stdout)) { close(fd); return 70; }
  // Parent owns the stdin pipe. EOF releases the kernel lock on host exit.
  while (getchar() != EOF) {}
  close(fd);
  return 0;
}
int main(int argc, char **argv) {
  if (argc != 3 || (strcmp(argv[1], "read") && strcmp(argv[1], "write") && strcmp(argv[1], "lock"))) return 64;
  if (strlen(argv[2]) != 64) return 64;
  for (int i = 0; i < 64; i++) if (!isxdigit((unsigned char)argv[2][i])) return 64;
  if (!strcmp(argv[1], "lock")) return own_store(argv[2]);
  SecKeychainSetUserInteractionAllowed(false);
  CFStringRef account = CFStringCreateWithCString(NULL, argv[2], kCFStringEncodingUTF8);
  const void *keys[] = { kSecClass, kSecAttrService, kSecAttrAccount };
  const void *values[] = { kSecClassGenericPassword, CFSTR("org.johnason.dsh.subscriptions"), account };
  CFMutableDictionaryRef query = CFDictionaryCreateMutable(NULL, 0,
    &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
  for (int i = 0; i < 3; i++) CFDictionarySetValue(query, keys[i], values[i]);
  OSStatus status;
  if (!strcmp(argv[1], "read")) {
    CFDictionarySetValue(query, kSecReturnData, kCFBooleanTrue);
    CFDictionarySetValue(query, kSecMatchLimit, kSecMatchLimitOne);
    CFTypeRef result = NULL;
    status = SecItemCopyMatching(query, &result);
    if (status == errSecSuccess) {
      if (result == NULL || CFGetTypeID(result) != CFDataGetTypeID()
          || CFDataGetLength(result) > LIMIT) status = errSecDecode;
      else {
        const CFIndex n = CFDataGetLength(result);
        if (fwrite(CFDataGetBytePtr(result), 1, n, stdout) != (size_t)n) status = errSecIO;
      }
    }
    if (result) CFRelease(result);
  } else {
    unsigned char *buffer = calloc(LIMIT + 1, 1);
    if (!buffer) return 71;
    size_t n = fread(buffer, 1, LIMIT + 1, stdin);
    if (n == 0 || n > LIMIT || ferror(stdin)) { free(buffer); return 65; }
    CFDataRef data = CFDataCreate(NULL, buffer, n);
    wipe(buffer, LIMIT + 1); free(buffer);
    CFMutableDictionaryRef update = CFDictionaryCreateMutable(NULL, 0,
      &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDictionarySetValue(update, kSecValueData, data);
    status = SecItemUpdate(query, update);
    if (status == errSecItemNotFound) {
      CFDictionarySetValue(query, kSecValueData, data);
      CFDictionarySetValue(query, kSecAttrLabel, CFSTR("Johnason DSH subscription login"));
      status = SecItemAdd(query, NULL);
      if (status == errSecDuplicateItem) {
        CFDictionaryRemoveValue(query, kSecValueData);
        CFDictionaryRemoveValue(query, kSecAttrLabel);
        status = SecItemUpdate(query, update);
      }
    }
    CFRelease(update); CFRelease(data);
  }
  CFRelease(query); CFRelease(account);
  return status_code(status);
}
