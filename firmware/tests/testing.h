// A deliberately tiny test harness for the firmware's host-side unit tests.
//
//   TEST(name) { CHECK(cond); CHECK_EQ(a, b); }
//
// Every TEST in the linked files runs from testing_main.cpp. A failed CHECK prints the
// location and the test keeps going, so one run reports every failure.

#ifndef SCOUT_TESTING_H
#define SCOUT_TESTING_H

#include <cstdio>
#include <sstream>
#include <string>

namespace testing {

typedef void (*TestFn)();

struct Registrar {
  Registrar(const char* name, TestFn fn);
};

void fail(const char* file, int line, const std::string& message);

template <typename A, typename B>
void checkEq(const A& a, const B& b, const char* expr_a, const char* expr_b, const char* file,
             int line) {
  if (!(a == b)) {
    std::ostringstream out;
    out << expr_a << " == " << expr_b << "  (got " << a << " vs " << b << ")";
    fail(file, line, out.str());
  }
}

// Root of the repository, for tests that read shared files such as protocol/uart_vectors.txt.
std::string repoPath(const std::string& relative);

}  // namespace testing

#define TEST(name)                                                   \
  static void name();                                                \
  static testing::Registrar name##_registrar(#name, name);           \
  static void name()

#define CHECK(cond)                                                  \
  do {                                                               \
    if (!(cond)) testing::fail(__FILE__, __LINE__, #cond);           \
  } while (0)

#define CHECK_EQ(a, b) testing::checkEq((a), (b), #a, #b, __FILE__, __LINE__)

#endif  // SCOUT_TESTING_H
