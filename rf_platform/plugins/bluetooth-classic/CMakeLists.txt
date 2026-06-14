project(btcexplorer-sniffer)
cmake_minimum_required(VERSION 3.0)

list(INSERT CMAKE_MODULE_PATH 0 ${CMAKE_SOURCE_DIR}/cmake/Modules)

set(BOOST_REQUIRED_COMPONENTS
    program_options
    system
    thread
)

find_package(Boost "1.46" REQUIRED ${BOOST_REQUIRED_COMPONENTS})
#find_package(UHD "3.9.7" REQUIRED)

set (CMAKE_CXX_STANDARD 11)

include_directories(
    ${Boost_INCLUDE_DIRS}
)
link_directories(${Boost_LIBRARY_DIRS})

add_executable(btcexplorer-sniffer src/btsniffer.cpp src/lapnode.cpp)

target_link_libraries(btcexplorer-sniffer ${Boost_LIBRARIES} SoapySDR fftw3f)

add_executable(btcexplorer-sniffer-gateway src/btsniffer.cpp src/lapnode.cpp)
target_compile_definitions(btcexplorer-sniffer-gateway PRIVATE BTC_GATEWAY_FIFO=1)
target_link_libraries(btcexplorer-sniffer-gateway ${Boost_LIBRARIES} SoapySDR fftw3f)
