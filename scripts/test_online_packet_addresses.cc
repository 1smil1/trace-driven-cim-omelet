// Real registry regression: destination bits must not waste global slots.
#include <cassert>
#include <iostream>
#include <set>
#include "mem/ruby/network/garnet/OnlinePacketRegistry.hh"

int main()
{
    using gem5::OnlinePacketRegistry;
    constexpr unsigned destinations = 10400;
    constexpr unsigned blockBits = 6;
    constexpr uint64_t memoryBytes = 512ULL * 1024 * 1024;
    std::set<gem5::Addr> addresses;
    for (unsigned destination = 0; destination < destinations; ++destination) {
        for (unsigned packet = 0; packet < 2; ++packet) {
            const uint64_t id = destination * 2ULL + packet;
            const auto address = OnlinePacketRegistry::registerPacket(
                id, 1024, destination, destinations, blockBits, memoryBytes);
            assert(address < memoryBytes);
            assert(((address >> blockBits) & 16383) == destination);
            assert(addresses.insert(address).second);
            uint64_t bytes = 0;
            assert(OnlinePacketRegistry::sizeFor(address, bytes) && bytes == 1024);
        }
    }
    assert(OnlinePacketRegistry::inFlight() == destinations * 2);
    bool rejected = false;
    try {
        OnlinePacketRegistry::registerPacket(999999, 1, 0, destinations,
                                            blockBits, 64);
    } catch (const std::overflow_error &) {
        rejected = true;
    }
    assert(rejected); // Range checking is retained, not bypassed.
    std::cout << "online addresses OK: 20800 unique packets in unchanged range\n";
}
