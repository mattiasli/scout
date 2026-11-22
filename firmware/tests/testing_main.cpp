#include <cstdlib>
#include <vector>

#include "testing.h"

namespace testing {

namespace {

struct Entry {
  const char* name;
  TestFn fn;
};

std::vector<Entry>& registry() {
  static std::vector<Entry> tests;
  return tests;
}

int failures = 0;
const char* current = "";

}  // namespace

Registrar::Registrar(const char* name, TestFn fn) { registry().push_back(Entry{name, fn}); }

void fail(const char* file, int line, const std::string& message) {
  ++failures;
  std::fprintf(stderr, "FAIL %s  %s:%d  %s\n", current, file, line, message.c_str());
}

std::string repoPath(const std::string& relative) {
  return std::string(SCOUT_REPO_ROOT) + "/" + relative;
}

}  // namespace testing

int main() {
  int failedTests = 0;
  for (const testing::Entry& test : testing::registry()) {
    testing::current = test.name;
    int before = testing::failures;
    test.fn();
    if (testing::failures != before) ++failedTests;
  }
  std::printf("%zu tests, %d failed\n", testing::registry().size(), failedTests);
  return failedTests == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
