cmake_minimum_required(VERSION 3.10)
project(mmdbake CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(BULLET_SRC ${CMAKE_SOURCE_DIR}/third_party/bullet3/src)
set(SABA_DIR   ${CMAKE_SOURCE_DIR}/third_party/saba)

# ---- Bullet (only the parts MMD physics needs) ----
file(GLOB_RECURSE BULLET_SOURCES
    ${BULLET_SRC}/LinearMath/*.cpp
    ${BULLET_SRC}/BulletCollision/*.cpp
    ${BULLET_SRC}/BulletDynamics/*.cpp)
add_library(bullet STATIC ${BULLET_SOURCES})
target_include_directories(bullet PUBLIC ${BULLET_SRC})
target_compile_definitions(bullet PUBLIC BT_THREADSAFE=1)
target_compile_options(bullet PRIVATE -w)

# ---- saba (MMD model / motion / physics) ----
file(GLOB SABA_SOURCES
    ${SABA_DIR}/src/Saba/Base/*.cpp
    ${SABA_DIR}/src/Saba/Model/MMD/*.cpp)
add_library(saba STATIC ${SABA_SOURCES})
target_include_directories(saba PUBLIC
    ${SABA_DIR}/src
    ${SABA_DIR}/external/glm/include
    ${SABA_DIR}/external/spdlog/include)
target_compile_definitions(saba PUBLIC UNICODE _UNICODE GLM_ENABLE_EXPERIMENTAL)
target_compile_options(saba PRIVATE -w)
target_link_libraries(saba PUBLIC bullet)

# ---- mmdbake CLI ----
add_executable(mmdbake src/main.cpp src/scene.cpp)
target_link_libraries(mmdbake PRIVATE saba)
target_link_options(mmdbake PRIVATE -static -municode)
