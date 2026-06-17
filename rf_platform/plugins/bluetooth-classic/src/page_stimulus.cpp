#include <SoapySDR/Device.hpp>
#include <SoapySDR/Formats.hpp>
#include <SoapySDR/Types.hpp>

#include <boost/program_options.hpp>
#include <boost/format.hpp>

#define _USE_MATH_DEFINES
#include <math.h>
#include <stdint.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <complex>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace po = boost::program_options;

typedef std::complex<float> cf32;

static std::atomic<bool> g_stop(false);

static void sigint_handler(int)
{
    g_stop = true;
}

static uint64_t compute_remainder(uint64_t dividend, uint64_t divisor)
{
    if (divisor == 0) return 0;
    int dividend_len = 64 - __builtin_clzll(dividend);
    int divisor_len = 64 - __builtin_clzll(divisor);
    while (dividend_len >= divisor_len && dividend != 0) {
        dividend ^= divisor << (dividend_len - divisor_len);
        dividend_len = dividend ? 64 - __builtin_clzll(dividend) : 0;
    }
    return dividend;
}

static uint32_t parse_hex_u32(const std::string &text, int bits, const std::string &name)
{
    std::string clean;
    for (char ch : text) {
        if (std::isxdigit((unsigned char) ch)) clean.push_back((char) std::toupper((unsigned char) ch));
    }
    if (clean.empty()) throw std::runtime_error(name + " is empty");
    uint32_t value = (uint32_t) std::stoul(clean, nullptr, 16);
    const uint32_t limit = bits >= 32 ? 0xffffffffu : ((1u << bits) - 1u);
    if (value > limit) throw std::runtime_error(name + " does not fit in requested bit width");
    return value;
}

static uint64_t bluetooth_sync_word_from_lap(uint32_t lap)
{
    const uint64_t barker = ((lap & 0x800000u) != 0) ? 0x13u : 0x2cu;
    const uint64_t x = (barker << 24) | (uint64_t) (lap & 0xffffffu);
    const uint64_t p = 0x83848D96BBCC54FCULL;
    const uint64_t xtilde = (p >> 34) ^ x;
    const uint64_t gp = 0157464165547ULL;
    const uint64_t g = (gp << 1) ^ gp;
    const uint64_t ctilde = compute_remainder(xtilde, g);
    const uint64_t stilde = ctilde | (xtilde << 34);
    return stilde ^ p;
}

static std::vector<int> device_access_code_bits(uint32_t lap)
{
    const uint64_t sync = bluetooth_sync_word_from_lap(lap);
    const int first_sync_bit = (int) (sync & 0x1u);
    const int preamble[4] = {
        first_sync_bit ? 0 : 1,
        first_sync_bit ? 1 : 0,
        first_sync_bit ? 0 : 1,
        first_sync_bit ? 1 : 0,
    };
    std::vector<int> bits;
    bits.reserve(72);
    for (int idx = 0; idx < 4; idx++) bits.push_back(preamble[idx]);
    for (int idx = 0; idx < 64; idx++) bits.push_back((sync >> idx) & 0x1u);
    const int last_sync_bit = (int) ((sync >> 63) & 0x1u);
    const int trailer[4] = {
        last_sync_bit ? 1 : 0,
        last_sync_bit ? 0 : 1,
        last_sync_bit ? 1 : 0,
        last_sync_bit ? 0 : 1,
    };
    for (int idx = 0; idx < 4; idx++) bits.push_back(trailer[idx]);
    return bits;
}

static std::vector<cf32> make_gfsk_like_id_packet(
    const std::vector<int> &bits,
    int sample_rate_sps,
    float amplitude,
    bool invert_fsk,
    double guard_us,
    bool shaped_edges)
{
    const int sps = std::max(2, (int) std::llround((double) sample_rate_sps / 1000000.0));
    const double actual_symbol_rate = (double) sample_rate_sps / (double) sps;
    const double freq_dev_hz = 160000.0;
    const double phase_step = 2.0 * M_PI * freq_dev_hz / (double) sample_rate_sps;
    std::vector<double> shaped;
    shaped.reserve(bits.size() * (size_t) sps);
    double last = bits.empty() ? 1.0 : (bits.front() ? 1.0 : -1.0);
    for (size_t bit_idx = 0; bit_idx < bits.size(); bit_idx++) {
        const double current = (bits[bit_idx] ? 1.0 : -1.0) * (invert_fsk ? -1.0 : 1.0);
        for (int n = 0; n < sps; n++) {
            if (shaped_edges) {
                const double t = (double) n / (double) sps;
                const double w = 0.5 - 0.5 * cos(M_PI * t);
                shaped.push_back(last + (current - last) * w);
            } else {
                shaped.push_back(current);
            }
        }
        last = current;
    }

    std::vector<cf32> out;
    out.reserve(shaped.size());
    double phase = 0.0;
    for (double symbol : shaped) {
        phase += phase_step * symbol * (1000000.0 / actual_symbol_rate);
        if (phase > M_PI) phase -= 2.0 * M_PI;
        if (phase < -M_PI) phase += 2.0 * M_PI;
        out.emplace_back(amplitude * (float) cos(phase), amplitude * (float) sin(phase));
    }
    const long long guard_count = std::max(0LL, std::llround((double) sample_rate_sps * guard_us / 1000000.0));
    const size_t guard_samples = (size_t) guard_count;
    for (size_t idx = 0; idx < guard_samples; idx++) out.emplace_back(0.0f, 0.0f);
    return out;
}

static std::vector<unsigned int> parse_channels(const std::string &text)
{
    std::vector<unsigned int> channels;
    if (text.empty() || text == "all") {
        for (unsigned int ch = 0; ch <= 78; ch++) channels.push_back(ch);
        return channels;
    }
    std::stringstream ss(text);
    std::string item;
    while (std::getline(ss, item, ',')) {
        if (item.empty()) continue;
        const size_t dash = item.find('-');
        if (dash != std::string::npos) {
            unsigned int first = (unsigned int) std::stoul(item.substr(0, dash));
            unsigned int last = (unsigned int) std::stoul(item.substr(dash + 1));
            if (first > last) std::swap(first, last);
            for (unsigned int ch = first; ch <= last; ch++) {
                if (ch <= 78) channels.push_back(ch);
            }
        } else {
            unsigned int ch = (unsigned int) std::stoul(item);
            if (ch <= 78) channels.push_back(ch);
        }
    }
    std::sort(channels.begin(), channels.end());
    channels.erase(std::unique(channels.begin(), channels.end()), channels.end());
    if (channels.empty()) throw std::runtime_error("no valid Bluetooth Classic channels selected");
    return channels;
}

static SoapySDR::Kwargs make_device_args(const std::string &driver, const std::string &device_id)
{
    SoapySDR::Kwargs query;
    query["driver"] = driver;
    std::vector<SoapySDR::Kwargs> devices = SoapySDR::Device::enumerate(query);
    size_t requested_index = 0;
    const size_t colon = device_id.find(':');
    if (colon != std::string::npos && colon + 1 < device_id.size()) {
        try {
            requested_index = (size_t) std::stoul(device_id.substr(colon + 1));
        } catch (...) {
            requested_index = 0;
        }
    }
    if (!devices.empty()) {
        if (requested_index >= devices.size()) {
            throw std::runtime_error((boost::format("requested %s but only %u %s device(s) enumerated")
                                      % device_id % devices.size() % driver).str());
        }
        return devices[requested_index];
    }
    return query;
}

static std::string kwargs_summary(const SoapySDR::Kwargs &args)
{
    std::string out;
    for (SoapySDR::Kwargs::const_iterator it = args.begin(); it != args.end(); ++it) {
        if (!out.empty()) out += ",";
        out += it->first + "=" + it->second;
    }
    return out;
}

static long long monotonic_ns()
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

int main(int argc, char **argv)
{
    std::string driver = "hackrf";
    std::string device_id = "hackrf:0";
    std::string lap_text;
    std::string channel_text = "all";
    double seconds = 10.0;
    double dwell_ms = 6.0;
    double guard_us = 80.0;
    int sample_rate_sps = 4000000;
    double tx_gain_db = 0.0;
    double tx_vga_gain_db = -1.0;
    float amplitude = 0.35f;
    std::string fsk_polarity = "auto";
    std::string edge_mode = "hard";
    bool lab_authorized = false;
    bool dry_run = false;

    po::options_description desc("Bluetooth Classic lab page stimulus transmitter");
    desc.add_options()
        ("help,h", "show help")
        ("lab-authorized", po::bool_switch(&lab_authorized)->default_value(false), "required: confirms this is an owned/authorized lab target")
        ("driver", po::value<std::string>(&driver)->default_value(driver), "SoapySDR driver")
        ("device-id", po::value<std::string>(&device_id)->default_value(device_id), "device id, for example hackrf:0")
        ("target-lap", po::value<std::string>(&lap_text)->required(), "target LAP as 6 hex chars")
        ("channels", po::value<std::string>(&channel_text)->default_value(channel_text), "Bluetooth channels: all, 0-78, or comma/range list")
        ("seconds", po::value<double>(&seconds)->default_value(seconds), "maximum transmit duration")
        ("dwell-ms", po::value<double>(&dwell_ms)->default_value(dwell_ms), "time to repeat ID bursts per channel")
        ("guard-us", po::value<double>(&guard_us)->default_value(guard_us), "zero-amplitude guard after each ID burst")
        ("sample-rate-sps", po::value<int>(&sample_rate_sps)->default_value(sample_rate_sps), "TX sample rate")
        ("tx-gain-db", po::value<double>(&tx_gain_db)->default_value(tx_gain_db), "TX gain where supported")
        ("tx-vga-gain-db", po::value<double>(&tx_vga_gain_db)->default_value(tx_vga_gain_db), "TX VGA gain where supported; negative disables named VGA override")
        ("amplitude", po::value<float>(&amplitude)->default_value(amplitude), "baseband amplitude 0..1")
        ("fsk-polarity", po::value<std::string>(&fsk_polarity)->default_value(fsk_polarity), "normal, inverted, or auto")
        ("edge-mode", po::value<std::string>(&edge_mode)->default_value(edge_mode), "hard or shaped")
        ("dry-run", po::bool_switch(&dry_run)->default_value(false), "print plan but do not transmit");

    po::variables_map vm;
    po::store(po::parse_command_line(argc, argv, desc), vm);
    if (vm.count("help")) {
        std::cout << desc << std::endl;
        return 0;
    }
    po::notify(vm);

    if (!lab_authorized) {
        throw std::runtime_error("--lab-authorized is required for active RF page stimulus");
    }
    if (seconds <= 0.0 || seconds > 60.0) {
        throw std::runtime_error("--seconds must be >0 and <=60");
    }
    if (dwell_ms <= 0.0 || dwell_ms > 100.0) {
        throw std::runtime_error("--dwell-ms must be >0 and <=100");
    }
    if (guard_us < 0.0 || guard_us > 2000.0) {
        throw std::runtime_error("--guard-us must be between 0 and 2000");
    }
    if (sample_rate_sps < 2000000 || sample_rate_sps > 20000000) {
        throw std::runtime_error("--sample-rate-sps must be between 2 Msps and 20 Msps");
    }
    if (amplitude <= 0.0f || amplitude > 1.0f) {
        throw std::runtime_error("--amplitude must be in 0..1");
    }
    std::transform(fsk_polarity.begin(), fsk_polarity.end(), fsk_polarity.begin(), [](char ch) {
        return (char) std::tolower((unsigned char) ch);
    });
    if (fsk_polarity != "normal" && fsk_polarity != "inverted" && fsk_polarity != "auto") {
        throw std::runtime_error("--fsk-polarity must be normal, inverted, or auto");
    }
    std::transform(edge_mode.begin(), edge_mode.end(), edge_mode.begin(), [](char ch) {
        return (char) std::tolower((unsigned char) ch);
    });
    if (edge_mode != "hard" && edge_mode != "shaped") {
        throw std::runtime_error("--edge-mode must be hard or shaped");
    }
    const bool shaped_edges = edge_mode == "shaped";

    const uint32_t lap = parse_hex_u32(lap_text, 24, "target LAP");
    std::vector<unsigned int> channels = parse_channels(channel_text);
    const std::vector<int> bits = device_access_code_bits(lap);
    std::vector<std::vector<cf32>> packets;
    std::vector<std::string> packet_names;
    if (fsk_polarity == "normal" || fsk_polarity == "auto") {
        packets.push_back(make_gfsk_like_id_packet(bits, sample_rate_sps, amplitude, false, guard_us, shaped_edges));
        packet_names.push_back("normal");
    }
    if (fsk_polarity == "inverted" || fsk_polarity == "auto") {
        packets.push_back(make_gfsk_like_id_packet(bits, sample_rate_sps, amplitude, true, guard_us, shaped_edges));
        packet_names.push_back("inverted");
    }

    std::cerr << boost::format("page_stimulus target_lap=%06X device=%s driver=%s channels=%u sr=%d dwell_ms=%.3f guard_us=%.1f polarity=%s edge=%s seconds=%.3f")
        % lap % device_id % driver % channels.size() % sample_rate_sps % dwell_ms % guard_us % fsk_polarity % edge_mode % seconds << std::endl;

    if (dry_run) {
        std::cerr << "dry_run=1 no RF transmitted" << std::endl;
        return 0;
    }

    std::signal(SIGINT, sigint_handler);
    std::signal(SIGTERM, sigint_handler);

    SoapySDR::Kwargs args = make_device_args(driver, device_id);
    std::cerr << "page_stimulus soapy_args=" << kwargs_summary(args) << std::endl;
    SoapySDR::Device *sdr = SoapySDR::Device::make(args);
    if (sdr == NULL) throw std::runtime_error("SoapySDR::Device::make failed");

    SoapySDR::Stream *tx_stream = NULL;
    try {
        sdr->setSampleRate(SOAPY_SDR_TX, 0, sample_rate_sps);
        try {
            std::vector<std::string> gains = sdr->listGains(SOAPY_SDR_TX, 0);
            std::string gain_names;
            for (size_t idx = 0; idx < gains.size(); idx++) {
                if (!gain_names.empty()) gain_names += ",";
                gain_names += gains[idx];
            }
            std::cerr << "page_stimulus tx_gain_names=" << (gain_names.empty() ? "(none)" : gain_names) << std::endl;
        } catch (const std::exception &exc) {
            std::cerr << "warning: TX gain listing failed: " << exc.what() << std::endl;
        }
        try {
            sdr->setGain(SOAPY_SDR_TX, 0, tx_gain_db);
            std::cerr << boost::format("page_stimulus tx_gain=%.1f") % tx_gain_db << std::endl;
        } catch (const std::exception &exc) {
            std::cerr << "warning: TX gain not applied: " << exc.what() << std::endl;
        }
        if (tx_vga_gain_db >= 0.0) {
            try {
                sdr->setGain(SOAPY_SDR_TX, 0, "VGA", tx_vga_gain_db);
                std::cerr << boost::format("page_stimulus tx_vga_gain=%.1f") % tx_vga_gain_db << std::endl;
            } catch (const std::exception &exc) {
                std::cerr << "warning: TX VGA gain not applied: " << exc.what() << std::endl;
            }
        }
        tx_stream = sdr->setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32);
        sdr->activateStream(tx_stream);

        const long long wall_start_ns = monotonic_ns();
        size_t bursts = 0;

        while (!g_stop) {
            const long long elapsed_ns = monotonic_ns() - wall_start_ns;
            if ((double) elapsed_ns / 1000000000.0 >= seconds) break;
            for (unsigned int ch : channels) {
                if (g_stop) break;
                const double freq_hz = 2402000000.0 + (double) ch * 1000000.0;
                sdr->setFrequency(SOAPY_SDR_TX, 0, freq_hz);
                usleep(900);
                const long long channel_start = monotonic_ns();
                const long long dwell_ns = (long long) (dwell_ms * 1000000.0);
                while (!g_stop && monotonic_ns() - channel_start < dwell_ns) {
                    const size_t packet_index = bursts % packets.size();
                    const std::vector<cf32> &packet = packets[packet_index];
                    void *buffs[] = {(void *) packet.data()};
                    int flags = 0;
                    const int ret = sdr->writeStream(tx_stream, buffs, (int) packet.size(), flags, 0, 100000);
                    if (ret < 0) {
                        throw std::runtime_error((boost::format("writeStream failed: %d") % ret).str());
                    }
                    bursts++;
                    usleep(160);
                }
            }
        }
        std::cerr << boost::format("page_stimulus complete bursts=%u") % bursts << std::endl;
        sdr->deactivateStream(tx_stream);
        sdr->closeStream(tx_stream);
        SoapySDR::Device::unmake(sdr);
    } catch (...) {
        if (tx_stream != NULL) {
            try { sdr->deactivateStream(tx_stream); } catch (...) {}
            try { sdr->closeStream(tx_stream); } catch (...) {}
        }
        SoapySDR::Device::unmake(sdr);
        throw;
    }
    return 0;
}
