// Standalone check: copies stay independent without copying every bit array.
#include <cstdlib>
#include <new>
#include "mem/ruby/common/TypeDefines.hh"
#include "mem/ruby/common/Set.hh"

static std::size_t allocated = 0;
void *operator new(std::size_t size)
{
    allocated += size;
    if (void *ptr = std::malloc(size ? size : 1))
        return ptr;
    throw std::bad_alloc();
}
void operator delete(void *ptr) noexcept { std::free(ptr); }
void operator delete(void *ptr, std::size_t) noexcept { std::free(ptr); }

int main()
{
    using gem5::ruby::RubyDynamicBitset;
    const auto empty_before = allocated;
    std::vector<RubyDynamicBitset> zero_sets(1000, RubyDynamicBitset(109504));
    for (auto &bits : zero_sets) {
        assert(bits.size() == 109504 && bits.none() && bits.count() == 0);
        assert(!bits.test(109503));
        bits.set(64, false);
        bits.reset(64);
        bits.reset();
        bits.resize(109504);
    }
    assert(allocated - empty_before < 65536); // Eager words require >13 MiB.
    RubyDynamicBitset zero(8192), written_zero(8192);
    written_zero.set(64);
    written_zero.reset(64);
    assert(zero == written_zero && !(zero & written_zero).intersects(zero));
    assert((~zero).count() == 8192);
    zero |= written_zero;
    assert(zero.none());
    written_zero.set(0);
    zero |= written_zero;
    zero.reset(0);
    assert(zero.none() && written_zero.test(0));
    assert((zero & written_zero).none() && (zero | written_zero).test(0));
    RubyDynamicBitset original(8192);
    original.set(0);
    original.set(64);
    const auto before = allocated;
    std::vector<RubyDynamicBitset> copies(512, original);
    assert(allocated - before < 65536); // Deep copies require >512 KiB.
    assert(copies[0] == original);
    copies[0][64] = false;
    assert(original.test(64) && !copies[0].test(64));
    copies[1].reset();
    assert(copies[1].none() && original.count() == 2);
    copies[2].set();
    assert(copies[2].count() == 8192 && original.count() == 2);
    copies[3].resize(128);
    assert(copies[3].none() && original.size() == 8192);
    RubyDynamicBitset right(8192);
    right.set(8191);
    copies[4] |= right;
    assert(copies[4].count() == 3 && !original.test(8191));
    copies[5] &= right;
    assert(copies[5].none() && original.count() == 2);
    assert(!original.intersects(right));
    right.set(64);
    assert(original.intersects(right));
    assert((original & right).count() == 1);
    assert((original | right).count() == 3);
    assert((~original).count() == 8190);
    copies[6] &= copies[6];
    assert(copies[6] == original);
    copies[7] |= copies[7];
    assert(copies[7] == original);
    copies[8].reset(0);
    assert(!copies[8].test(0) && original.test(0));
    RubyDynamicBitset empty;
    assert(empty.none() && empty.count() == 0 && !empty.intersects(empty));
    std::cout << "Ruby bitset storage OK: independent copies, mutations and intersections\n";
}
