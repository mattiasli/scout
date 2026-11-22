# Host-side unit tests for the firmware. Nothing here needs a board.
#
#   cmake -S firmware/tests -B build/firmware-tests
#   cmake --build build/firmware-tests
#   ctest --test-dir build/firmware-tests --output-on-failure
#
# Each suite globs its own folder, so adding a test file needs no edit here.
#   scoutlink/  -> tests for firmware/libraries/ScoutLink
#   uno/        -> tests for the board-independent code in firmware/scout_uno/src/core
#   esp32/      -> tests for the board-independent code in firmware/scout_esp32cam/src/core

cmake_minimum_required(VERSION 3.16)
project(scout_firmware_tests CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

get_filename_component(REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
set(FIRMWARE "${REPO_ROOT}/firmware")

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Werror -Wno-unused-parameter)
endif()

add_library(testing STATIC testing_main.cpp)
target_include_directories(testing PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")
target_compile_definitions(testing PUBLIC SCOUT_REPO_ROOT="${REPO_ROOT}" SCOUT_HOST_TEST=1)

add_library(scoutlink STATIC "${FIRMWARE}/libraries/ScoutLink/src/ScoutLink.cpp")
target_include_directories(scoutlink PUBLIC "${FIRMWARE}/libraries/ScoutLink/src")

enable_testing()

function(scout_suite name test_dir source_dir)
  file(GLOB tests CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/${test_dir}/*.cpp")
  if(NOT tests)
    return()
  endif()
  set(sources "")
  if(source_dir)
    file(GLOB_RECURSE sources CONFIGURE_DEPENDS "${source_dir}/*.cpp")
  endif()
  add_executable(${name} ${tests} ${sources})
  target_link_libraries(${name} PRIVATE testing scoutlink)
  if(source_dir)
    target_include_directories(${name} PRIVATE "${source_dir}")
  endif()
  add_test(NAME ${name} COMMAND ${name})
endfunction()

scout_suite(scoutlink_tests scoutlink "")
scout_suite(uno_tests uno "${FIRMWARE}/scout_uno/src/core")
scout_suite(esp32_tests esp32 "${FIRMWARE}/scout_esp32cam/src/core")
