
#include <boost/program_options.hpp>
#include <boost/format.hpp>
#include <boost/algorithm/string.hpp>
#include <string>
#include <iostream>
#include <cstdlib>
#include <pthread.h>
#include <csignal>
#include <stdio.h>
#include <sys/time.h>
#include <climits>
#define _USE_MATH_DEFINES
#include <math.h>
#include <unordered_map>
#include "lapnode.hpp"
#include <complex.h>

namespace po = boost::program_options;

typedef std::complex<float> iqsamp_t;

/*** Select number of channels ***/
#define CHAN_8
#include "btsniffer.hpp"

#include <iostream>
#include <stdexcept>

#include <cstdio>	//stdandard output
#include <cstdlib>

#include <SoapySDR/Device.hpp>
#include <SoapySDR/Types.hpp>
#include <SoapySDR/Formats.hpp>
#include <fftw3.h>

#include <string>	// std::string
#include <vector>	// std::vector<...>
#include <map>		// std::map< ... , ... >
#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstring>
#include <unistd.h>

#include <iostream>



/*!
 * Defines a safe wrapper that places a catch-all around main.
 * If an exception is thrown, it prints to stderr and returns.
 * Usage: int UHD_SAFE_MAIN(int argc, char *argv[]){ main code here }
 * \param _argc the declaration for argc
 * \param _argv the declaration for argv
 */
#define SAFE_MAIN(_argc, _argv)                               \
    _main(int, char* []);                                         \
    int main(int argc, char* argv[])                              \
{                                                             \
    try {                                                     \
    return _main(argc, argv);                             \
    } catch (const std::exception& e) {                       \
    if (g_log != NULL) { log_event(std::string("error ") + e.what()); fclose(g_log); g_log = NULL; } \
    std::cerr << "Error: " << e.what() << std::endl;      \
    } catch (...) {                                           \
    if (g_log != NULL) { log_event("error unknown exception"); fclose(g_log); g_log = NULL; } \
    std::cerr << "Error: unknown exception" << std::endl; \
    }                                                         \
    return ~0;                                                \
    }                                                             \
    int _main(_argc, _argv)







unsigned int decfactor = 8;

std::vector<double> make_filter_taps(unsigned int bins)
{
    if (bins == 8) {
        return std::vector<double>(filter_taps, filter_taps + FILTER_TAP_NUM);
    }

    const size_t ntaps = 2 * bins + 1;
    const double fc = 0.5 / (double) bins;
    const double mid = ((double) ntaps - 1.0) / 2.0;
    std::vector<double> taps(ntaps);
    double sum = 0.0;

    for (size_t n = 0; n < ntaps; n++) {
        const double x = (double) n - mid;
        const double sinc = (fabs(x) < 1e-12)
                ? 2.0 * fc
                : sin(2.0 * M_PI * fc * x) / (M_PI * x);
        const double window = 0.54 - 0.46 * cos((2.0 * M_PI * (double) n) / ((double) ntaps - 1.0));
        taps[n] = sinc * window;
        sum += taps[n];
    }

    if (fabs(sum) > 1e-12) {
        for (size_t n = 0; n < ntaps; n++) taps[n] /= sum;
    }
    return taps;
}

unsigned long total_num_samps = 0;

volatile unsigned int bufselect;
pthread_spinlock_t lock[2];
std::atomic<bool> bankA_ready(false);

volatile static bool stopsig = false;
void sigint_handler(int) {stopsig = true;}

FILE *g_log = NULL;
FILE *g_events = NULL;
pthread_mutex_t g_log_lock = PTHREAD_MUTEX_INITIALIZER;
pthread_mutex_t g_events_lock = PTHREAD_MUTEX_INITIALIZER;
bool g_show_init_failed = false;
bool g_record_only = false;
bool g_jsonl_stdout = false;

long long now_us()
{
    struct timeval now;
    gettimeofday(&now, NULL);
    return ((long long) now.tv_sec * 1000000LL) + (long long) now.tv_usec;
}

void log_event(const std::string &message)
{
    if (g_log == NULL) return;

    struct timeval now;
    gettimeofday(&now, NULL);

    pthread_mutex_lock(&g_log_lock);
    fprintf(g_log, "%ld.%06ld %s\n", now.tv_sec, now.tv_usec, message.c_str());
    fflush(g_log);
    pthread_mutex_unlock(&g_log_lock);
}

void log_event(const boost::format &message)
{
    log_event(message.str());
}

void emit_json_event(const std::string &json)
{
    FILE *out = g_jsonl_stdout ? stdout : g_events;
    if (out == NULL) return;
    pthread_mutex_lock(&g_events_lock);
    fprintf(out, "%s\n", json.c_str());
    fflush(out);
    pthread_mutex_unlock(&g_events_lock);
}

std::string json_quote(const std::string &value)
{
    std::string out;
    out.reserve(value.size() + 2);
    out.push_back('"');
    for (size_t i = 0; i < value.size(); i++) {
        const char c = value[i];
        if (c == '"' || c == '\\') {
            out.push_back('\\');
            out.push_back(c);
        } else if (c == '\n') {
            out += "\\n";
        } else if (c == '\r') {
            out += "\\r";
        } else if (c == '\t') {
            out += "\\t";
        } else {
            out.push_back(c);
        }
    }
    out.push_back('"');
    return out;
}

typedef struct ProcPars {
    iqsamp_t *bankA;
    iqsamp_t *bankB;
    size_t bufsize;
} proc_pars_t;

inline bool is_valid_preamble(uint8_t *binbuf, unsigned int k)
{
    uint8_t preamble1 = 0, preamble2 = 0;
    preamble1 = binbuf[k] + binbuf[k+2*srate];
    preamble2 = binbuf[k+1*srate] + binbuf[k+3*srate];

    if ((preamble1 == 2 && preamble2 == 0) or (preamble1 == 0 && preamble2 == 2))
        return true;
    else
        return false;
}

inline uint8_t extract_byte(uint8_t *binbuf, unsigned int start)
{
    uint8_t result = 0x00;
    for (int b = 0; b < 8; b++) result |= binbuf[start + b*srate] << b;
    return result;
}

double estimate_packet_rssi_db(iqsamp_t *chan, size_t start, size_t bufsize)
{
    const size_t stop = std::min(bufsize, start + (size_t) 160 * srate);
    if (start >= stop) return -120.0;
    double power = 0.0;
    size_t count = 0;
    for (size_t k = start; k < stop; k++) {
        power += (chan[k].real() * chan[k].real()) + (chan[k].imag() * chan[k].imag());
        count++;
    }
    if (count == 0) return -120.0;
    // FFTW's forward transform is not normalized, so each 1 MHz lane carries
    // decfactor gain. Convert back to a dBFS-like scale: full-scale power is 0 dB,
    // silence/zero power is floored at -120 dB.
    const double fft_gain = std::max(1.0, (double) decfactor);
    const double normalized_power = (power / (double) count) / (fft_gain * fft_gain);
    const double dbfs = 10.0 * log10(normalized_power + 1e-12);
    if (dbfs > 0.0) return 0.0;
    if (dbfs < -120.0) return -120.0;
    return dbfs;
}

struct timeval t_start;

void compute_tsdiff(struct timeval *x, struct timeval *y, struct timeval *diff)
{
    // x must be bigger than y
    int x_usec = x->tv_usec;
    int x_sec = x->tv_sec;
    int y_usec = y->tv_usec;
    int y_sec = y->tv_sec;

    if (x_usec < y_usec) {
        x_sec --;
        x_usec += 1000000;
    }

    diff->tv_sec = x_sec - y_sec;
    diff->tv_usec = x_usec - y_usec;

    return;
}


int _length (uint64_t word, int left, int right)
{
    if (left == right)
        return left;

    int mid = (left + right) / 2;
    if (right == mid)
        return mid;

    if (word >= (1LLU << mid))
        return (_length (word, left, mid));

    return (_length (word, mid, right));
}


int length (uint64_t word)
{
    return (_length (word, 64, 0));
}

std::unordered_map<uint32_t, lap_node> lap_map;
std::unordered_map<uint32_t, uint32_t> solved_lap_uap_map;
std::unordered_map<uint32_t, uint64_t> passive_bdaddr_map;
std::unordered_map<uint32_t, long long> init_fail_log_ts_map;
std::unordered_map<uint32_t, unsigned int> init_fail_suppressed_map;
struct header_sense_stats {
    unsigned int observations;
    int score;
};
std::unordered_map<uint64_t, header_sense_stats> header_sense_map;
const unsigned int HEADER_SENSE_MIN_OBSERVATIONS = 3;
const int HEADER_SENSE_MIN_MARGIN = 8;
const long long INIT_FAIL_CONSOLE_INTERVAL_US = 20000;
const size_t FHS_PAYLOAD_BYTES = 18;
int fec23_codewords[32768];

int extract_header_bf(uint8_t *buf, uint32_t* head, uint32_t *clks, int *clk_found, uint32_t uap);

double parse_mhz_value(std::string value)
{
    value.erase(std::remove_if(value.begin(), value.end(), [](char c) {
        return std::isspace((unsigned char)c);
    }), value.end());
    std::transform(value.begin(), value.end(), value.begin(), [](char c) {
        return (char) std::tolower((unsigned char)c);
    });

    double scale = 1.0;
    if (value.size() > 3 && value.substr(value.size() - 3) == "mhz") {
        value.erase(value.size() - 3);
    } else if (value.size() > 2 && value.substr(value.size() - 2) == "hz") {
        value.erase(value.size() - 2);
        scale = 1.0e-6;
    }

    if (value.empty()) {
        throw std::runtime_error("empty MHz value");
    }
    return std::stod(value) * scale;
}

double parse_legacy_hz_to_mhz(std::string value)
{
    value.erase(std::remove_if(value.begin(), value.end(), [](char c) {
        return std::isspace((unsigned char)c);
    }), value.end());
    std::string lower = value;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](char c) {
        return (char) std::tolower((unsigned char)c);
    });
    if (lower.size() > 3 && lower.substr(lower.size() - 3) == "mhz") {
        return parse_mhz_value(value);
    }
    if (lower.size() > 2 && lower.substr(lower.size() - 2) == "hz") {
        return parse_mhz_value(value);
    }
    if (value.empty()) {
        throw std::runtime_error("empty Hz value");
    }
    return std::stod(value) / 1.0e6;
}

std::string remaining_uaps_for_lap(lap_node &node)
{
    std::string out;
    for (int idx = 0; idx < 32; idx++) {
        uint32_t clks[2];
        uint32_t uap;
        bool valid;
        int clk_index;
        node.get_uap_data(idx, &valid, &uap, &clks[0], &clks[1], &clk_index);
        if (!valid) continue;
        if (!out.empty()) out += " ";
        out += (boost::format("%02X") % uap).str();
    }
    return out;
}

uint64_t header_sense_key(uint32_t lap, uint32_t uap)
{
    return (((uint64_t) lap) << 8) | (uint64_t) (uap & 0xff);
}

uint32_t dewhiten_header(uint32_t header, uint32_t clk)
{
    uint32_t header_dewhiten = header;
    uint32_t whitener = (clk & 0x3f) | 0x40;

    for (int i = 0; i < 18; i++) {
        uint32_t whitener_out = (whitener >> 6) & 0x1;
        uint32_t whitener_shifted = (whitener << 1) & 0x7f;
        whitener = whitener_shifted ^ (whitener_out | (whitener_out << 4));
        header_dewhiten = header_dewhiten ^ (whitener_out << i);
    }
    return header_dewhiten;
}

bool is_plausible_acl_type(uint32_t type)
{
    switch (type) {
    case 0x0: // NULL
    case 0x1: // POLL
    case 0x2: // FHS
    case 0x3: // DM1
    case 0x4: // DH1 / 2-DH1
    case 0x8: // DV / 3-DH1
    case 0x9: // AUX1
    case 0xa: // DM3 / 2-DH3
    case 0xb: // DH3 / 3-DH3
    case 0xe: // DM5 / 2-DH5
    case 0xf: // DH5 / 3-DH5
        return true;
    default:
        return false;
    }
}

int header_sense_score(uint32_t dewhitened_header)
{
    const uint32_t lt_addr = dewhitened_header & 0x7;
    const uint32_t type = (dewhitened_header >> 3) & 0xf;
    const uint32_t flow = (dewhitened_header >> 7) & 0x1;
    int score = 0;

    score += (lt_addr != 0) ? 3 : -4;
    score += flow ? 2 : -2;
    score += is_plausible_acl_type(type) ? 1 : -1;
    return score;
}

std::string describe_header_sense(uint32_t uap, uint32_t clk, uint32_t dewhitened_header,
                                  const header_sense_stats &stats)
{
    const uint32_t lt_addr = dewhitened_header & 0x7;
    const uint32_t type = (dewhitened_header >> 3) & 0xf;
    const uint32_t flow = (dewhitened_header >> 7) & 0x1;
    const uint32_t arqn = (dewhitened_header >> 8) & 0x1;
    const uint32_t seqn = (dewhitened_header >> 9) & 0x1;

    return (boost::format("%02X clk=%02u hd=%03X lt=%u type=%X flow=%u arqn=%u seqn=%u score=%d obs=%u")
            % uap % clk % (dewhitened_header & 0x3ffff)
            % lt_addr % type % flow % arqn % seqn
            % stats.score % stats.observations).str();
}

uint8_t reverse_bits8(uint8_t value)
{
    value = (uint8_t) (((value & 0xf0) >> 4) | ((value & 0x0f) << 4));
    value = (uint8_t) (((value & 0xcc) >> 2) | ((value & 0x33) << 2));
    value = (uint8_t) (((value & 0xaa) >> 1) | ((value & 0x55) << 1));
    return value;
}

void fec23_init(void)
{
    for (int kk = 0; kk < 32768; kk++) fec23_codewords[kk] = -1;

    for (int data = 0; data < 1024; data++) {
        uint32_t fec = 0;
        for (int bit = 0; bit < 10; bit++) {
            const uint32_t data_in = (data >> bit) & 0x1;
            const uint32_t fec_out = (fec >> 4) & 0x1;
            const uint32_t data_out = data_in ^ fec_out;
            const uint32_t fec_adder = (data_out << 4) | (data_out << 2) | data_out;
            fec = ((fec << 1) & 0x1f) ^ fec_adder;
        }
        fec = reverse_bits8((uint8_t) fec) >> 3;
        const uint32_t codeword = (fec << 10) | (uint32_t) data;
        if (codeword & ~0x7fff) throw std::runtime_error("FEC 2/3 map generation overflow");
        if (fec23_codewords[codeword] != -1) throw std::runtime_error("FEC 2/3 map collision");
        fec23_codewords[codeword] = data;

        for (int bit = 0; bit < 15; bit++) {
            const uint32_t codeword_err = codeword ^ (1u << bit);
            if (codeword_err & ~0x7fff) throw std::runtime_error("FEC 2/3 error-map overflow");
            if (fec23_codewords[codeword_err] != -1) throw std::runtime_error("FEC 2/3 error-map collision");
            fec23_codewords[codeword_err] = data;
        }
    }
}

uint32_t whiten_next(uint32_t *whitener)
{
    const uint32_t whitener_out = (*whitener >> 6) & 0x1;
    const uint32_t whitener_shifted = (*whitener << 1) & 0x7f;
    *whitener = whitener_shifted ^ (whitener_out | (whitener_out << 4));
    return whitener_out;
}

uint32_t payload_bits_le(const uint8_t *payload, unsigned int bit_offset, unsigned int bit_count)
{
    uint32_t value = 0;
    for (unsigned int bit = 0; bit < bit_count; bit++) {
        const unsigned int source_bit = bit_offset + bit;
        const uint32_t bit_value = (payload[source_bit / 8] >> (source_bit % 8)) & 0x1;
        value |= bit_value << bit;
    }
    return value;
}

std::string payload_hex(const uint8_t *payload, size_t payload_len)
{
    std::string out;
    for (size_t idx = 0; idx < payload_len; idx++) {
        out += (boost::format("%02X") % (unsigned int) payload[idx]).str();
    }
    return out;
}

bool is_inquiry_access_lap(uint32_t lap)
{
    // GIAC is 0x9E8B33; DIAC values share the 0x9E8B00..0x9E8B3f LAP range.
    return (lap & 0xffffc0) == 0x9e8b00;
}

struct fhs_decode_result {
    uint32_t lap;
    uint32_t uap;
    uint32_t nap;
    uint32_t clk;
    uint32_t header;
    bool payload_whitener_after_header;
    int errors;
    uint8_t payload[FHS_PAYLOAD_BYTES];
};

struct fhs_decode_stats {
    long long attempts;
    long long inquiry_attempts;
    long long solved_lap_attempts;
    long long truncated;
    long long header_matches;
    long long type_matches;
    long long payload_decodes;
    long long fec_rejects;
    long long address_rejects;
    long long packet_types[16];
};

bool extract_payload_fec23(uint8_t *binbuf, size_t payload_start, size_t bufsize, uint32_t clk,
                           bool whitener_after_header, uint8_t *payload, int *errors)
{
    const int maxlen_bit = (int) FHS_PAYLOAD_BYTES * 8;
    const int block_count = (maxlen_bit + 9) / 10;
    const size_t encoded_bits = (size_t) block_count * 15;
    if (payload_start + (encoded_bits - 1) * srate >= bufsize) return false;

    memset(payload, 0, FHS_PAYLOAD_BYTES);
    *errors = 0;

    uint32_t whitener = (clk & 0x3f) | 0x40;
    if (whitener_after_header) {
        for (int bit = 0; bit < 18; bit++) whiten_next(&whitener);
    }

    for (int block = 0; block < block_count; block++) {
        uint32_t codeword_rx = 0;
        for (int bit = 0; bit < 15; bit++) {
            const size_t coded_bit_number = (size_t) block * 15 + bit;
            const uint32_t bit_read = binbuf[payload_start + coded_bit_number * srate] ? 1 : 0;
            codeword_rx |= bit_read << bit;
        }

        if (fec23_codewords[codeword_rx] == -1) {
            (*errors)++;
            continue;
        }

        const uint32_t correct_data = (uint32_t) fec23_codewords[codeword_rx];
        for (int bit = 0; bit < 10; bit++) {
            const int payload_bit = block * 10 + bit;
            const uint32_t bit_whitened = (correct_data >> bit) & 0x1;
            const uint32_t bit_dewhitened = bit_whitened ^ whiten_next(&whitener);
            if (payload_bit >= maxlen_bit) continue;
            payload[payload_bit / 8] |= bit_dewhitened << (payload_bit % 8);
        }
    }

    return true;
}

bool try_decode_fhs_at(uint8_t *binbuf, size_t access_start, size_t bufsize,
                       bool require_expected, uint32_t expected_lap, uint32_t expected_uap,
                       fhs_decode_result *result, fhs_decode_stats *stats)
{
    stats->attempts++;
    if (require_expected) stats->solved_lap_attempts++;
    else stats->inquiry_attempts++;
    const size_t header_start = access_start + 72 * srate;
    const size_t payload_start = access_start + (72 + 54) * srate;
    if (payload_start >= bufsize || header_start + 53 * srate >= bufsize) {
        stats->truncated++;
        return false;
    }

    uint32_t header = 0;
    uint32_t clk_table[64];
    int clk_found = 0;

    // Once UAP:LAP is solved, testing only that UAP is both faster and less
    // likely to admit an unrelated header during the passive NAP search.
    const uint32_t first_uap = require_expected ? expected_uap : 0;
    const uint32_t last_uap = require_expected ? expected_uap : 255;
    for (uint32_t uap = first_uap; uap <= last_uap; uap++) {
        const int found = extract_header_bf(binbuf + header_start, &header, clk_table, &clk_found, uap);
        if (found != 2) continue;
        stats->header_matches++;

        for (int clk_idx = 0; clk_idx < found; clk_idx++) {
            const uint32_t clk = clk_table[clk_idx];
            const uint32_t dewhitened_header = dewhiten_header(header, clk);
            const uint32_t packet_type = (dewhitened_header >> 3) & 0xf;
            stats->packet_types[packet_type]++;
            if (packet_type != 0x2) continue; // FHS
            stats->type_matches++;

            for (int mode = 0; mode < 2; mode++) {
                uint8_t payload[FHS_PAYLOAD_BYTES];
                int errors = 0;
                const bool after_header = (mode == 1);
                if (!extract_payload_fec23(binbuf, payload_start, bufsize, clk, after_header, payload, &errors)) {
                    stats->truncated++;
                    continue;
                }
                stats->payload_decodes++;
                if (errors != 0) {
                    stats->fec_rejects++;
                    continue;
                }

                const uint32_t fhs_lap = payload_bits_le(payload, 34, 24);
                const uint32_t fhs_uap = payload_bits_le(payload, 64, 8);
                const uint32_t fhs_nap = payload_bits_le(payload, 72, 16);

                if (fhs_uap != uap ||
                        (require_expected && (fhs_lap != expected_lap || fhs_uap != expected_uap)) ||
                        fhs_lap == 0 || is_inquiry_access_lap(fhs_lap)) {
                    stats->address_rejects++;
                    continue;
                }

                result->lap = fhs_lap;
                result->uap = fhs_uap;
                result->nap = fhs_nap;
                result->clk = clk;
                result->header = dewhitened_header;
                result->payload_whitener_after_header = after_header;
                result->errors = errors;
                memcpy(result->payload, payload, FHS_PAYLOAD_BYTES);
                return true;
            }
        }
    }

    return false;
}

bool record_passive_bdaddr(const fhs_decode_result &fhs, uint32_t access_lap, unsigned int channel,
                           long long ts_us, double rssi_dbfs, FILE *fptrout)
{
    const uint64_t bdaddr = ((uint64_t) fhs.nap << 32) | ((uint64_t) fhs.uap << 24) | fhs.lap;
    const std::string address = (boost::format("%02X:%02X:%02X:%02X:%02X:%02X")
                                 % ((fhs.nap >> 8) & 0xff) % (fhs.nap & 0xff) % fhs.uap
                                 % ((fhs.lap >> 16) & 0xff) % ((fhs.lap >> 8) & 0xff) % (fhs.lap & 0xff)).str();

    std::unordered_map<uint32_t, uint64_t>::iterator existing = passive_bdaddr_map.find(fhs.lap);
    if (existing != passive_bdaddr_map.end() && existing->second == bdaddr) return false;

    passive_bdaddr_map[fhs.lap] = bdaddr;
    solved_lap_uap_map[fhs.lap] = fhs.uap;

    std::cout << boost::format("[%2u] %12lld us -- %06X -- PASSIVE FHS BD_ADDR %s")
                 % channel % ts_us % access_lap % address << std::endl;
    log_event(boost::format("passive fhs bdaddr address=%s nap=%04X uap=%02X lap=%06X access_lap=%06X channel=%u ts_us=%lld clk=%02u header=%03X whitening=%s payload=%s")
              % address % fhs.nap % fhs.uap % fhs.lap % access_lap % channel % ts_us % fhs.clk
              % (fhs.header & 0x3ffff)
              % (fhs.payload_whitener_after_header ? "after-header" : "fresh")
              % payload_hex(fhs.payload, FHS_PAYLOAD_BYTES));
    emit_json_event((boost::format("{\"time_us\":%lld,\"type\":\"passive_fhs_bdaddr\",\"address\":%s,\"nap\":\"%04X\",\"uap\":\"%02X\",\"lap\":\"%06X\",\"access_lap\":\"%06X\",\"channel\":%u,\"ts_us\":%lld,\"clk\":%u,\"rssi_dbfs\":%.2f}")
                     % now_us() % json_quote(address) % fhs.nap % fhs.uap % fhs.lap % access_lap % channel % ts_us % fhs.clk % rssi_dbfs).str());

    if (fptrout != NULL) {
        fprintf(fptrout, "%lld %06X -- PASSIVE FHS BD_ADDR %s -- access_lap %06X -- channel %u\n",
                ts_us, fhs.lap, address.c_str(), access_lap, channel);
        fflush(fptrout);
    }

    return true;
}

uint64_t compute_remainder (uint64_t input, uint64_t divisor)
{
    int divisor_length = length (divisor);
    int input_length = length (input);

    if (divisor_length + input_length > 63)
        return input;

    input = input << divisor_length;

    while (length (input) >= divisor_length) {
        uint64_t tmp = divisor << (length (input) - divisor_length);
        input = input ^ tmp;
    }

    return input;
}

/* Extracts the header if FEC is perfect, then it tries (bruteforce)
 * all possible clk values to dewhiten the header and finally
 * tries all possible UAP until a valid HEC code is found.
 */
int extract_header_bf (uint8_t *buf, uint32_t* head, uint32_t *clks, int *clk_found, uint32_t uap)
{
    int perfect_rx = 0;
    uint32_t header = 0;

    for (int i = 0; i < 54; i += 3) {
        int s0 = 0, s1 = 0;
        for (int j = 0; j < 3; j++) {
            if ( *(buf+(i+j)*srate) ) s1++;
            else s0++;
        }
        header >>= 1;
        if (s1 == 0 || s0 == 0) perfect_rx++;
        if (s1 > s0) header |= 0x20000;
    }

    *head = header;

    if (perfect_rx != 18) return -1;

    // first bit received is the LSB, so header is composed by
    //     +-----+---------+
    // MSB | HEC | LT_ADDR | LSB
    //     +-----+---------+
    //      8 bit   10 bit

    // Brute force the clock
    *clk_found = 0;
    for (uint32_t clk = 0; clk < 64; clk++) {
        uint32_t header_dewhiten = header;
        uint32_t whitener;
        whitener = (clk & 0x3f) | 0x40;

        // Dewhiten header using this clk value
        for (int i = 0; i < 18; i++) {
            uint32_t whitener_out = (whitener >> 6) & 0x1;
            uint32_t whitener_shifted = (whitener << 1) & 0x7f;
            whitener = whitener_shifted ^ (whitener_out | (whitener_out << 4));
            header_dewhiten = header_dewhiten ^ (whitener_out << i);
        }

        // Re-compute the HEC over the dewhitened header
        uint32_t lfsr = uap;
        for (int i = 0; i < 10; i++) {
            uint32_t lfsr_out = (lfsr >> 7) & 0x1;
            uint32_t data_in = (header_dewhiten >> i) & 0x1;
            uint32_t lfsr_in = (lfsr_out ^ data_in);
            uint32_t lfsr_adder =
                    (lfsr_in << 7) |
                    (lfsr_in << 5) |
                    (lfsr_in << 2) |
                    (lfsr_in << 1) |
                    (lfsr_in << 0);
            lfsr = (lfsr << 1) & 0xff;
            lfsr = lfsr ^ lfsr_adder;
        }

        // Compare HEC computed and HEC received.
        // First bit received is in header_dewhiten[10], last is in header_dewhiten[17].
        // First bit to be transmitted is in position 7, last one is in position 0.
        int kk = 0;
        while (kk < 8) {
            uint32_t bit_rx = (header_dewhiten >> (10 + kk)) & 0x1;
            uint32_t bit_tx = (lfsr >> (7 - kk)) & 0x1;
            if (bit_rx != bit_tx) break;
            kk++;
        }

        if (kk == 8) {
            clks[*clk_found] = clk;
            (*clk_found)++;
        }
    }

    return *clk_found;
}


void* proc_routine(void *routine_params)
{
    // Set priority on current thread
    //    uhd::set_thread_priority_safe(1, true);
    printf("setting up proc thread\n");
    log_event("processing thread starting");
    // Read parameters.
    proc_pars_t *pars = (proc_pars_t *) routine_params;
    size_t bufsize = pars->bufsize;
    iqsamp_t *bankA = pars->bankA;
    iqsamp_t *bankB = pars->bankB;

    iqsamp_t *curbuf = bankA;
    size_t samples_processed = 0;
    long long packets_seen = 0;
    long long preamble_hits = 0;
    long long barker_hits = 0;
    long long access_hits = 0;
    long long access_rejects = 0;
    long long lap_events = 0;
    long long resolved_events = 0;
    long long fhs_events = 0;
    fhs_decode_stats fhs_stats = {};
    long long last_metrics_us = now_us();
    FILE *fptrout = fopen("results.txt","w");
    if (fptrout == NULL) {
        log_event("failed to open results.txt for writing; processing thread exiting");
        stopsig = true;
        return NULL;
    }

    // Allocate buffers and auxiliary pointers.
    uint8_t *binbuffer = (uint8_t*) malloc(decfactor * bufsize * sizeof(uint8_t));
    iqsamp_t *sigbuf = (iqsamp_t*) malloc(decfactor * bufsize * sizeof(iqsamp_t));
    iqsamp_t *chanbuf = (iqsamp_t*) malloc(decfactor * bufsize * sizeof(iqsamp_t));

    const bool fft_channelizer = decfactor > 16;

    // Make filter polyphase. The original 8-bin path stays intact for HackRF.
    std::vector<double> taps;
    std::vector<std::vector<double>> poly;
    std::vector<std::vector<iqsamp_t>> twiddle;
    size_t components_length = 0;
    if (!fft_channelizer) {
        taps = make_filter_taps(decfactor);
        poly.resize(decfactor);
        components_length = (size_t) ceil((double) taps.size() / (double) decfactor);
        for (size_t i = 0; i < components_length * decfactor; i++) {
            if (i < taps.size()) poly[i%decfactor].push_back(taps[i]);
            else poly[i%decfactor].push_back(0.0);
        }

        std::complex<float> i_unit(0.0f, 1.0f);
        twiddle.resize(decfactor);
        for (size_t row = 0; row < decfactor; row++) {
            for (size_t col = 0; col < decfactor; col++) {
                iqsamp_t tmp =
                        exp(-i_unit * (float)(2.0*M_PI * ((double) col*row) / (double)decfactor));
                twiddle[row].push_back(tmp);
            }
        }
    }

    fftwf_complex *fft_in = NULL;
    fftwf_complex *fft_out = NULL;
    fftwf_plan fft_plan = NULL;
    if (fft_channelizer) {
        fft_in = (fftwf_complex*) fftwf_malloc(sizeof(fftwf_complex) * decfactor);
        fft_out = (fftwf_complex*) fftwf_malloc(sizeof(fftwf_complex) * decfactor);
        if (fft_in == NULL || fft_out == NULL) {
            throw std::runtime_error("Failed to allocate FFT channelizer buffers");
        }
        fft_plan = fftwf_plan_dft_1d((int) decfactor, fft_in, fft_out, FFTW_FORWARD, FFTW_MEASURE);
        std::cout << boost::format("Using FFT channelizer with %u 1 MHz bins") % decfactor << std::endl;
        log_event(boost::format("channelizer fft bins=%u") % decfactor);
    } else {
        log_event(boost::format("channelizer polyphase bins=%u taps=%u") % decfactor % taps.size());
    }

    while(stopsig == false) {
        while (!stopsig && !bankA_ready.load()) {
            usleep(1000);
        }
        if (stopsig) break;

        const size_t blocksize = 1000; // us
        const size_t blocknsamps = blocksize*srate; // samples per block
        const size_t nblocks = floor(bufsize/blocknsamps);

        memset(chanbuf, 0, decfactor * bufsize * sizeof(iqsamp_t));
        if (fft_channelizer) {
            for (unsigned int i = 0; i < bufsize; i++) {
                const size_t raw_offset = (size_t) i * decfactor;
                for (size_t k = 0; k < decfactor; k++) {
                    fft_in[k][0] = curbuf[raw_offset + k].real();
                    fft_in[k][1] = curbuf[raw_offset + k].imag();
                }
                fftwf_execute(fft_plan);
                for (size_t ch = 0; ch < decfactor; ch++) {
                    const size_t fft_bin = (ch + decfactor / 2) % decfactor;
                    chanbuf[ch*bufsize + i] = iqsamp_t(fft_out[fft_bin][0], fft_out[fft_bin][1]);
                }
            }
        } else {
            memset(sigbuf, 0, decfactor * bufsize * sizeof(iqsamp_t));
            for (size_t i = taps.size()-1; i < bufsize*decfactor; i += decfactor) {
                for (size_t k = 0; k < components_length; k++) {
                    size_t idx = i/decfactor;
                    sigbuf[idx-1] += (curbuf[i-k*decfactor+0] * (float) poly[0].at(k))/((float) components_length);
                    for (size_t ch = 1; ch < decfactor; ch++) {
                        sigbuf[ch*bufsize + idx] += (curbuf[i-k*decfactor+(decfactor-ch)] * (float) poly[ch].at(k)/((float) components_length-1));
                    }
                }
            }

            for (unsigned int i = 0; i < bufsize; i++) {
                for (size_t ch = 0; ch < decfactor; ch++)
                    for (size_t k = 0; k < decfactor; k++)
                        chanbuf[ch*bufsize + i] += sigbuf[k*bufsize+i] * twiddle[ch].at(k);
            }
        }

        for (unsigned int ch=0; ch<decfactor; ch++) {
            iqsamp_t *chan = chanbuf + ch * bufsize;
            uint8_t *tmpbinbuf = binbuffer + ch * bufsize;

            // Discriminate bits without using atan2.
            for (size_t i = 1; i < bufsize; i++) {
                double tmp = chan[i-1].real() * chan[i].imag() - chan[i-1].imag() * chan[i].real();
                tmpbinbuf[i] = (tmp > 0) ? 1 : 0;
            }
        }

        for (size_t block = 1; block < nblocks-1; block++) {
//            printf("proc thread: block %d\n",block);
            for (unsigned int ch = 0; ch < decfactor; ch++) {
                iqsamp_t *chan = chanbuf + ch * bufsize;
                uint8_t *binbuf = binbuffer + ch * bufsize;

                for (unsigned int i = block*blocknsamps; i < (block+1)*blocknsamps; i++) {
                    if (is_valid_preamble(binbuf, i) == false) continue;
                    preamble_hits++;

                    uint64_t barker = extract_byte(binbuf, i + 62*srate);
                    barker = barker & 0x3f;
                    if (barker != 0x13 && barker != 0x2c) continue;
                    barker_hits++;

                    uint64_t lap =
                            (uint64_t) extract_byte(binbuf, i + 54*srate) << 16 |
                                                                             (uint64_t) extract_byte(binbuf, i + 46*srate) << 8  |
                                                                             (uint64_t) extract_byte(binbuf, i + 38*srate);

                    uint64_t code =
                            ((uint64_t) extract_byte(binbuf, i +  4*srate) <<  0) |
                            ((uint64_t) extract_byte(binbuf, i + 12*srate) <<  8) |
                            ((uint64_t) extract_byte(binbuf, i + 20*srate) << 16) |
                            ((uint64_t) extract_byte(binbuf, i + 28*srate) << 24) |
                            ((uint64_t) extract_byte(binbuf, i + 36*srate) << 32);
                    code = code & 0x3FFFFFFFFLLU;

                    uint64_t aw = ((uint64_t) barker << 58) | (lap << 34) | code;

                    // use lap to rebuild access word from scratch, do not use barker
                    // set barker accordingly to extracted lap.
                    uint64_t barker_true =  ((lap & 0x800000) != 0) ? 0x13 : 0x2c;

                    uint64_t x = (barker_true << 24) | lap;
                    uint64_t p = 0x83848D96BBCC54FC;
                    uint64_t xtilde = (p >> 34) ^ x;
                    uint64_t gp = 0157464165547;
                    uint64_t g = (gp << 1) ^ gp;
                    uint64_t ctilde = compute_remainder (xtilde, g);
                    uint64_t stilde = ctilde | (xtilde << 34);
                    uint64_t awfinal = stilde ^ p;

                    uint32_t _lap = (uint32_t) lap;
                    const double packet_rssi_dbfs = estimate_packet_rssi_db(chan, i, bufsize);
                    if (aw == awfinal) {
                        const long long packet_ts_us = (long long) (samples_processed + i) / srate;

                        if (is_inquiry_access_lap(_lap)) {
                            fhs_decode_result fhs;
                            if (try_decode_fhs_at(binbuf, i, bufsize, false, 0, 0, &fhs, &fhs_stats)) {
                                record_passive_bdaddr(fhs, _lap, ch, packet_ts_us, packet_rssi_dbfs, fptrout);
                                fhs_events++;
                            }
                            continue;
                        }

                        std::unordered_map<uint32_t, uint32_t>::iterator solved_it = solved_lap_uap_map.find(_lap);
                        if (solved_it != solved_lap_uap_map.end()) {
                            fhs_decode_result fhs;
                            if (try_decode_fhs_at(binbuf, i, bufsize, true, _lap, solved_it->second, &fhs, &fhs_stats)) {
                                record_passive_bdaddr(fhs, _lap, ch, packet_ts_us, packet_rssi_dbfs, fptrout);
                                fhs_events++;
                            }
                            continue;
                        }
                        if (lap_map.find(_lap) == lap_map.end()) {
                            lap_map[_lap] = lap_node(_lap);
                            log_event(boost::format("lap new lap=%06X channel=%u ts_us=%lld")
                                      % _lap % ch % packet_ts_us);
                        }
                        packets_seen++;
                        access_hits++;
                    } else {
                        access_rejects++;
                        continue;
                    }

                    if (stopsig) break;

#define INVALID_CLK_INDEX -1
#define DELTA_TS_SAME_THRESHOLD 40 // this should depend on frame length!
#define DELTA_TS_SLOT_THRESHOLD 620 // should be 625, give margin
#define SLOT_DURATION 625.0
#define ERROR_THRESHOLD 0.05

                    uint32_t header = 0;
                    uint32_t clk_table[64];
                    long long timenow_sec_us = (samples_processed + i)/srate;
                    bool prefix_printed = false;
                    auto print_prefix = [&]() {
                        if (!prefix_printed && stopsig == false) {
                            std::cout << boost::format("[%2d] %12lld us -- %06X -- ")
                                         % ch % (timenow_sec_us) % (_lap);
                            prefix_printed = true;
                        }
                    };

                    lap_map[_lap].increase_processed_packets();
                    switch (lap_map[_lap].get_status()) {
                    case LAP_STATE_NEW:
                    {
                        int a;
                        uint32_t uap;
                        lap_map[_lap].set_ts(timenow_sec_us);
                        lap_map[_lap].set_tstart(timenow_sec_us);
                        //std::cout << boost::format("started %lld -- ") % timenow_sec_us;
                        int valid_uaps = 0;
                        for (uap = 0; uap < 256; uap ++) {
                            int clk_found = extract_header_bf(binbuf+i+72*srate,
                                                              &header, clk_table, &a, uap);
                            if (clk_found <= 0) {
                                lap_map[_lap].bf_failed(); // don't log but keep trace of LAP
                                continue;
                            }
                            else if (clk_found != 2) {
                                // This should never happen.
                                print_prefix();
                                std::cerr << "Invalid number of clk values " << clk_found << std::endl;
                                stopsig = true;
                                continue;
                            }
                            lap_map[_lap].set_uap_data(valid_uaps, true, uap,
                                                       clk_table[0], clk_table[1], INVALID_CLK_INDEX);
                            valid_uaps++;
                        }
                        if (valid_uaps != 32) {
                            const long long last_log_ts = init_fail_log_ts_map[_lap];
                            const bool should_print =
                                    g_show_init_failed &&
                                    (last_log_ts == 0 ||
                                     timenow_sec_us - last_log_ts >= INIT_FAIL_CONSOLE_INTERVAL_US);
                            if (should_print) {
                                const unsigned int suppressed = init_fail_suppressed_map[_lap];
                                print_prefix();
                                std::cout << boost::format("Init failed");
                                if (suppressed > 0) {
                                    std::cout << boost::format(" (%u similar suppressed)") % suppressed;
                                }
                                std::cout << std::endl;
                                init_fail_log_ts_map[_lap] = timenow_sec_us;
                                init_fail_suppressed_map[_lap] = 0;
                            } else {
                                init_fail_suppressed_map[_lap]++;
                            }
                            log_event(boost::format("lap init failed lap=%06X channel=%u ts_us=%lld valid_uaps=%d")
                                      % _lap % ch % timenow_sec_us % valid_uaps);
                            lap_map[_lap].bf_cannot_init();
                        } else {
                            print_prefix();
                            std::cout << boost::format("Initialized") << std::endl;
                            init_fail_log_ts_map.erase(_lap);
                            init_fail_suppressed_map.erase(_lap);
                            lap_map[_lap].set_status(LAP_STATE_BRUTE_FORCING);
                            std::string initial_uaps = remaining_uaps_for_lap(lap_map[_lap]);
                            log_event(boost::format("lap initialized lap=%06X channel=%u ts_us=%lld uaps=[%s]")
                                      % _lap % ch % timenow_sec_us % initial_uaps);
                            emit_json_event((boost::format("{\"time_us\":%lld,\"type\":\"lap_initialized\",\"lap\":\"%06X\",\"channel\":%u,\"ts_us\":%lld,\"candidate_count\":32,\"uaps\":%s,\"rssi_dbfs\":%.2f}")
                                             % now_us() % _lap % ch % timenow_sec_us % json_quote(initial_uaps) % packet_rssi_dbfs).str());
                            lap_events++;
                        }
                    }
                        break;
                    case LAP_STATE_BRUTE_FORCING:
                    {
                        print_prefix();
                        long long tmpdeltats = timenow_sec_us - lap_map[_lap].get_ts();
                        if (tmpdeltats < 0) {
                            // Skip packet
                            std::cout << "Skip packet in the past" << std::endl;
                            log_event(boost::format("lap packet skipped past lap=%06X channel=%u ts_us=%lld delta_us=%lld")
                                      % _lap % ch % timenow_sec_us % tmpdeltats);
                            lap_map[_lap].count_packet_inthepast ();
                            continue;
                        }

                        if (llabs(lap_map[_lap].get_ts() - timenow_sec_us) < DELTA_TS_SAME_THRESHOLD) {
                            // This might happen with packet received on side channels.
                            std::cout << "Skip packet (too close to another one) ";
                            int a, clk_index;
                            int confirmed_uap = 0;
                            int valid_uap = 0;
                            for (int jj = 0; jj < 32; jj ++) {
                                uint32_t clks[2];
                                uint32_t uap;
                                bool uap_valid;
                                lap_map[_lap].get_uap_data(jj, &uap_valid, &uap,
                                                           &clks[0], &clks[1], &clk_index);
                                if(uap_valid == false) continue;
                                valid_uap ++;
                                int clk_found = extract_header_bf(binbuf+i+72*srate,
                                                                  &header, clk_table, &a, uap);
                                if (clk_found != 2) continue;
                                if (clk_table[0] != clks[0] || clk_table[1] != clks[1]) continue;
                                confirmed_uap ++;
                            }

                            if (confirmed_uap == valid_uap) lap_map[_lap].new_packet_too_close (true);
                            else lap_map[_lap].new_packet_too_close (false);

                            std::cout <<
                                         boost::format("Confirmed %d out of %d UAPs for LAP %0X. Skipping")
                                         % confirmed_uap % valid_uap % _lap << std::endl;
                            log_event(boost::format("lap packet skipped too-close lap=%06X channel=%u ts_us=%lld confirmed_uaps=%d valid_uaps=%d")
                                      % _lap % ch % timenow_sec_us % confirmed_uap % valid_uap);
                            lap_map[_lap].set_ts(timenow_sec_us);
                            continue;
                        } else if (llabs(lap_map[_lap].get_ts() - timenow_sec_us) < DELTA_TS_SLOT_THRESHOLD) {
                            // we cannot handle such situation at the moment, so simply exit
                            std::cerr << "Cannot handle packet type, removing LAP" << std::endl;
                            log_event(boost::format("lap removed close-slot lap=%06X channel=%u ts_us=%lld prev_ts_us=%lld")
                                      % _lap % ch % timenow_sec_us % lap_map[_lap].get_ts());
                            lap_map.erase(_lap);
                            continue;
                        } else {
                            // measure how far away we are from being a multiple of 625
                            long long prevts = lap_map[_lap].get_ts();
                            long long deltats = timenow_sec_us - prevts;
                            float deltats_float = (float) deltats;
                            float periods = deltats_float / 625.0;
                            float periods_round = roundf(periods);
                            float error = fabsf(periods - periods_round);
                            if (error > ERROR_THRESHOLD) {
                                std::cerr << boost::format("Error too big (%f), LAP removed") % error
                                          << std::endl;
                                log_event(boost::format("lap removed slot-error lap=%06X channel=%u ts_us=%lld prev_ts_us=%lld periods=%.3f error=%.6f")
                                          % _lap % ch % timenow_sec_us % prevts % periods % error);
                                lap_map.erase(_lap);
                                continue;
                            }

                            int a, clk_index;
                            int count_valid_uap = 0;
                            int count_broken_uap = 0;
                            for (int jj = 0; jj < 32; jj ++) {
                                uint32_t clks[2];
                                uint32_t uap;
                                bool uap_valid;
                                lap_map[_lap].get_uap_data(jj, &uap_valid, &uap,
                                                           &clks[0], &clks[1], &clk_index);
                                if(uap_valid == false)
                                    continue;
                                int clk_found = extract_header_bf(binbuf+i+72*srate,
                                                                  &header, clk_table, &a, uap);
                                if (clk_found != 2) {
                                    // if we end up here it means either
                                    // 1. the connection changed, we should remove the old one and restart
                                    // 2. this frame is corrupt
                                    // At the moment we handle case 2 only, so we simply ignore this uap
                                    // and we make sure at the end that all were broken
                                    count_broken_uap++;
                                    continue;
                                }
                                if (clk_table[0] == clks[0] && clk_table[1] == clks[1]) {
                                    count_valid_uap++;
                                    continue;
                                }
                                int slot = ((int) periods_round) % 64;
                                // test only the valid one
                                int old_to_check = 2;
                                if (clk_index != INVALID_CLK_INDEX) {
                                    clks[0] = clks[clk_index];
                                    old_to_check = 1;
                                }
                                int qnew, qold;
                                int qnew_valid = INVALID_CLK_INDEX;
                                int matched = 0;
                                for (qold = 0; qold < old_to_check; qold ++) {
                                    for (qnew = 0; qnew < 2; qnew ++) {
                                        int slot_guessed = (clk_table[qnew] - clks[qold]) % 64;
                                        if (slot == slot_guessed) {
                                            qnew_valid = qnew;
                                            matched ++;
                                        }
                                    }
                                }
                                if (matched > 1) {
                                    lap_map[_lap].set_uap_data(jj, true, uap, clk_table[0], clk_table[1],
                                            INVALID_CLK_INDEX);
                                    count_valid_uap ++;
                                } else if (matched > 0) {
                                    lap_map[_lap].set_uap_data(jj, true, uap, clk_table[0], clk_table[1],
                                            qnew_valid);
                                    count_valid_uap ++;
                                } else {
                                    lap_map[_lap].set_uap_data(jj, false, 0, 0, 0, INVALID_CLK_INDEX);
                                }
                            }
                            if (count_valid_uap == 0 && count_broken_uap > 0) {
                                lap_map[_lap].count_broken_uap ();
                                std::cerr << "Frame likely broken, skipped" << std::endl;
                                log_event(boost::format("lap frame broken lap=%06X channel=%u ts_us=%lld broken_uaps=%d")
                                          % _lap % ch % timenow_sec_us % count_broken_uap);
                                continue;
                            } else if (count_valid_uap == 0) {
                                std::cout << "No valid UAP remaining, LAP removed" << std::endl;
                                log_event(boost::format("lap removed no-uap lap=%06X channel=%u ts_us=%lld")
                                          % _lap % ch % timenow_sec_us);
                                lap_map.erase(_lap);
                                continue;
                            } else if (count_valid_uap <= 2) {
                                uint32_t clks[2];
                                uint32_t uap;
                                bool isvalid;
                                int clock_index;
                                uint32_t uap_found[2];
                                int uap_idx = 0;

                                struct timeval ts;
                                gettimeofday(&ts, NULL);

                                for (int i = 0; i < 32; i++) {
                                    lap_map[_lap].get_uap_data(i, &isvalid, &uap,
                                                               &clks[0], &clks[1], &clock_index);
                                    if (isvalid) {
                                        uap_found[uap_idx++] = uap;
                                    }
                                }

                                long long tsfirst = lap_map[_lap].get_tstart();
                                long long tsresolv = (samples_processed + i)/srate;
                                long long tsdiff = tsresolv - tsfirst;
                                bool resolved = (uap_idx == 1);
                                uint32_t resolved_uap = resolved ? uap_found[0] : 0;
                                std::string header_desc[2];

                                if (uap_idx == 2) {
                                    for (int candidate = 0; candidate < 2; candidate++) {
                                        uint32_t candidate_clks[2] = {0, 0};
                                        uint32_t candidate_uap = 0;
                                        bool candidate_valid = false;
                                        int candidate_clk_index = INVALID_CLK_INDEX;
                                        uint32_t best_clk = 0;
                                        uint32_t best_header = 0;
                                        int best_score = -1000;

                                        for (int scan = 0; scan < 32; scan++) {
                                            lap_map[_lap].get_uap_data(scan, &candidate_valid, &candidate_uap,
                                                                       &candidate_clks[0], &candidate_clks[1],
                                                                       &candidate_clk_index);
                                            if (!candidate_valid || candidate_uap != uap_found[candidate]) continue;

                                            const int clk_options = (candidate_clk_index == INVALID_CLK_INDEX) ? 2 : 1;
                                            for (int option = 0; option < clk_options; option++) {
                                                const int clk_slot = (candidate_clk_index == INVALID_CLK_INDEX)
                                                        ? option
                                                        : candidate_clk_index;
                                                const uint32_t dewhitened = dewhiten_header(header, candidate_clks[clk_slot]);
                                                const int score = header_sense_score(dewhitened);
                                                if (score > best_score) {
                                                    best_score = score;
                                                    best_clk = candidate_clks[clk_slot];
                                                    best_header = dewhitened;
                                                }
                                            }
                                            break;
                                        }

                                        header_sense_stats &stats = header_sense_map[header_sense_key(_lap, uap_found[candidate])];
                                        stats.observations++;
                                        stats.score += best_score;
                                        header_desc[candidate] =
                                                describe_header_sense(uap_found[candidate], best_clk, best_header, stats);
                                    }

                                    header_sense_stats stats0 = header_sense_map[header_sense_key(_lap, uap_found[0])];
                                    header_sense_stats stats1 = header_sense_map[header_sense_key(_lap, uap_found[1])];
                                    if (stats0.observations >= HEADER_SENSE_MIN_OBSERVATIONS &&
                                            stats1.observations >= HEADER_SENSE_MIN_OBSERVATIONS &&
                                            abs(stats0.score - stats1.score) >= HEADER_SENSE_MIN_MARGIN) {
                                        resolved = true;
                                        resolved_uap = (stats0.score > stats1.score) ? uap_found[0] : uap_found[1];
                                    }
                                }

                                if (resolved) {
                                    std::cout << boost::format("RESOLVED UAP:LAP %02X:%06X") % resolved_uap % _lap;
                                    log_event(boost::format("lap resolved lap=%06X uap=%02X channel=%u ts_us=%lld tracking_us=%lld%s%s%s")
                                              % _lap % resolved_uap % ch % tsresolv % tsdiff
                                              % (uap_idx == 2 ? " header-sense " : "")
                                              % (uap_idx == 2 ? header_desc[0] : "")
                                              % (uap_idx == 2 ? (" | " + header_desc[1]) : ""));
                                    emit_json_event((boost::format("{\"time_us\":%lld,\"type\":\"lap_resolved\",\"lap\":\"%06X\",\"uap\":\"%02X\",\"channel\":%u,\"ts_us\":%lld,\"tracking_us\":%lld,\"candidate_count\":1,\"rssi_dbfs\":%.2f}")
                                                     % now_us() % _lap % resolved_uap % ch % tsresolv % tsdiff % packet_rssi_dbfs).str());
                                    resolved_events++;
                                } else {
                                    std::cout << boost::format("Only two UAP left (%02X and %02X)")
                                                 % uap_found[0] % uap_found[1];
                                    log_event(boost::format("lap two-uap-left lap=%06X uap0=%02X uap1=%02X channel=%u ts_us=%lld tracking_us=%lld candidates=[%s] [%s]")
                                              % _lap % uap_found[0] % uap_found[1] % ch % tsresolv % tsdiff
                                              % header_desc[0] % header_desc[1]);
                                    emit_json_event((boost::format("{\"time_us\":%lld,\"type\":\"lap_two_uap_left\",\"lap\":\"%06X\",\"uap0\":\"%02X\",\"uap1\":\"%02X\",\"channel\":%u,\"ts_us\":%lld,\"tracking_us\":%lld,\"candidate_count\":2,\"candidate0\":%s,\"candidate1\":%s,\"rssi_dbfs\":%.2f}")
                                                     % now_us() % _lap % uap_found[0] % uap_found[1] % ch % tsresolv % tsdiff % json_quote(header_desc[0]) % json_quote(header_desc[1]) % packet_rssi_dbfs).str());
                                    lap_events++;
                                }
                                std::cout << " - ";

                                // Check that only 2 UAPs have been found.
                                if (uap_idx > 2)
                                    fprintf(fptrout, "ERROR\n");

                                /* Compute energy of last pkt */
                                double energy = 0;
                                for (size_t k = i; k < i + 64*srate; k++) {
                                    energy += (chan[k].real() * chan[k].real()) + (chan[k].imag() * chan[k].imag());
                                }

                                std::cout << boost::format("first: %lld -- ") % tsfirst;

                                std::cout << boost::format("tracking for %lld us")  % tsdiff;
                                std::cout << std::endl;

                                if (resolved) {
                                    fprintf(fptrout, "%ld.%06ld %06X -- RESOLVED UAP %02X",
                                            ts.tv_sec, ts.tv_usec, _lap, resolved_uap);
                                    fprintf(fptrout, " -- ts %lld -- tracking_us %lld -- energy %lf\n",
                                            tsresolv, tsdiff, energy);
                                    fflush (fptrout);
                                }

                                if (resolved) {
                                    solved_lap_uap_map[_lap] = resolved_uap;
                                    log_event(boost::format("lap marked solved lap=%06X uap=%02X solved_total=%u")
                                              % _lap % resolved_uap % solved_lap_uap_map.size());
                                    header_sense_map.erase(header_sense_key(_lap, uap_found[0]));
                                    if (uap_idx == 2) header_sense_map.erase(header_sense_key(_lap, uap_found[1]));
                                    lap_map.erase(_lap);
                                    continue;
                                }

                            } else {
                                std::string remaining_uaps = remaining_uaps_for_lap(lap_map[_lap]);
                                std::cout << boost::format("%d possible UAPs remaining [%s]")
                                             % count_valid_uap % remaining_uaps;
                                std::cout << std::endl;
                                log_event(boost::format("lap narrowed lap=%06X channel=%u ts_us=%lld valid_uaps=%d uaps=[%s]")
                                          % _lap % ch % timenow_sec_us % count_valid_uap % remaining_uaps);
                                emit_json_event((boost::format("{\"time_us\":%lld,\"type\":\"lap_narrowed\",\"lap\":\"%06X\",\"channel\":%u,\"ts_us\":%lld,\"candidate_count\":%d,\"uaps\":%s,\"rssi_dbfs\":%.2f}")
                                                 % now_us() % _lap % ch % timenow_sec_us % count_valid_uap % json_quote(remaining_uaps) % packet_rssi_dbfs).str());
                                lap_events++;
                            }

                            lap_map[_lap].set_ts(timenow_sec_us);
                        }
                    }
                        break;
                    default:
                    {
                    }
                        break;
                    }

                    i += 100;
                }
            }

        }

        // Increment sample count.
        samples_processed += bufsize;
        {
            const long long cur_us = now_us();
            if (cur_us - last_metrics_us >= 1000000LL) {
                emit_json_event((boost::format("{\"time_us\":%lld,\"type\":\"metrics\",\"samples_processed\":%llu,\"packets_seen\":%lld,\"preamble_hits\":%lld,\"barker_hits\":%lld,\"access_hits\":%lld,\"access_rejects\":%lld,\"lap_events\":%lld,\"resolved_events\":%lld,\"fhs_events\":%lld,\"fhs_attempts\":%lld,\"fhs_inquiry_attempts\":%lld,\"fhs_solved_lap_attempts\":%lld,\"fhs_truncated\":%lld,\"fhs_header_matches\":%lld,\"fhs_type_matches\":%lld,\"fhs_payload_decodes\":%lld,\"fhs_fec_rejects\":%lld,\"fhs_address_rejects\":%lld,\"fhs_packet_types\":[%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld],\"solved_laps\":%u,\"active_laps\":%u,\"bins\":%u}")
                                 % cur_us % (unsigned long long) samples_processed % packets_seen % preamble_hits % barker_hits % access_hits % access_rejects % lap_events % resolved_events % fhs_events
                                 % fhs_stats.attempts % fhs_stats.inquiry_attempts % fhs_stats.solved_lap_attempts
                                 % fhs_stats.truncated % fhs_stats.header_matches % fhs_stats.type_matches
                                 % fhs_stats.payload_decodes % fhs_stats.fec_rejects % fhs_stats.address_rejects
                                 % fhs_stats.packet_types[0] % fhs_stats.packet_types[1] % fhs_stats.packet_types[2] % fhs_stats.packet_types[3]
                                 % fhs_stats.packet_types[4] % fhs_stats.packet_types[5] % fhs_stats.packet_types[6] % fhs_stats.packet_types[7]
                                 % fhs_stats.packet_types[8] % fhs_stats.packet_types[9] % fhs_stats.packet_types[10] % fhs_stats.packet_types[11]
                                 % fhs_stats.packet_types[12] % fhs_stats.packet_types[13] % fhs_stats.packet_types[14] % fhs_stats.packet_types[15]
                                 % solved_lap_uap_map.size() % lap_map.size() % decfactor).str());
                last_metrics_us = cur_us;
            }
        }


        bankA_ready.store(false);
    }

    free(chanbuf);
    free(sigbuf);
    free(binbuffer);
    if (fft_plan != NULL) fftwf_destroy_plan(fft_plan);
    if (fft_in != NULL) fftwf_free(fft_in);
    if (fft_out != NULL) fftwf_free(fft_out);
    fclose(fptrout);

    return NULL;
}


// Entry point.
int SAFE_MAIN(int argc, char *argv[])
{
    std::string freq_mhz_arg = "2432";
    std::string bandwidth_mhz_arg = "8";
    double rate = 0.0;
    double freq = 0.0;
    double seconds = 2.0;
    unsigned int bins = 0;
    std::string driver = "hackrf";
    std::string fifo_path = "";
    std::string log_path = "btsniffer.log";
    std::string events_path = "";

    po::options_description desc("Allowed options");
    desc.add_options()
            ("help,h", "print help message")
            ("driver", po::value<std::string>(&driver)->default_value(driver), "SoapySDR driver to open")
            ("freq-mhz", po::value<std::string>(&freq_mhz_arg)->default_value(freq_mhz_arg), "RX center frequency in MHz, accepts 2432 or 2432MHz")
            ("bandwidth-mhz", po::value<std::string>(&bandwidth_mhz_arg)->default_value(bandwidth_mhz_arg), "whole-MHz capture width; also sets number of 1 MHz bins")
            ("freq", po::value<std::string>(), "deprecated alias for --freq-mhz")
            ("rate", po::value<std::string>(), "deprecated Hz sample-rate alias; converted to --bandwidth-mhz")
            ("bins", po::value<unsigned int>(), "deprecated alias for --bandwidth-mhz")
            ("seconds", po::value<double>(&seconds)->default_value(seconds), "seconds of IQ to buffer per processing pass")
            ("fifo", po::value<std::string>(&fifo_path)->default_value(fifo_path), "optional path for raw CF32 IQ output")
            ("log", po::value<std::string>(&log_path)->default_value(log_path), "diagnostic log path, overwritten each run")
            ("events", po::value<std::string>(&events_path)->default_value(events_path), "optional JSONL event path, overwritten each run")
            ("jsonl-stdout", po::bool_switch(&g_jsonl_stdout)->default_value(false), "write JSONL events to stdout for Python supervisors")
            ("show-init-failed", po::bool_switch(&g_show_init_failed)->default_value(false), "show init-failed LAP messages on stdout; always logged to file")
            ("record-only", po::bool_switch(&g_record_only)->default_value(false), "record raw IQ continuously and skip Bluetooth packet processing")
    ;
    po::variables_map vm;
    po::store(po::parse_command_line(argc, argv, desc), vm);
    po::notify(vm);
    if (vm.count("help")) {
        std::cout << desc << std::endl;
        return EXIT_SUCCESS;
    }
    if (vm.count("freq")) {
        freq_mhz_arg = vm["freq"].as<std::string>();
    }
    double legacy_rate_mhz = 0.0;
    bool legacy_rate_set = false;
    if (vm.count("rate")) {
        legacy_rate_mhz = parse_legacy_hz_to_mhz(vm["rate"].as<std::string>());
        bandwidth_mhz_arg = (boost::format("%.9f") % legacy_rate_mhz).str();
        legacy_rate_set = true;
    }
    if (vm.count("bins")) {
        bandwidth_mhz_arg = (boost::format("%u") % vm["bins"].as<unsigned int>()).str();
    }
    const double freq_mhz = parse_mhz_value(freq_mhz_arg);
    const double bandwidth_mhz = parse_mhz_value(bandwidth_mhz_arg);
    const double rounded_bandwidth_mhz = floor(bandwidth_mhz + 0.5);
    if (fabs(bandwidth_mhz - rounded_bandwidth_mhz) > 1e-6) {
        throw std::runtime_error("--bandwidth-mhz must be a whole number of MHz");
    }
    if (rounded_bandwidth_mhz < 2.0) {
        throw std::runtime_error("--bandwidth-mhz must be at least 2");
    }
    if (rounded_bandwidth_mhz > (double) UINT_MAX) {
        throw std::runtime_error("--bandwidth-mhz is too large");
    }
    bins = (unsigned int) rounded_bandwidth_mhz;
    decfactor = bins;
    rate = (double) bins * 1.0e6;
    freq = freq_mhz * 1.0e6;

    g_log = fopen(log_path.c_str(), "w");
    if (g_log == NULL) {
        throw std::runtime_error("Failed to open log file for writing: " + log_path);
    }
    if (!events_path.empty()) {
        g_events = fopen(events_path.c_str(), "w");
        if (g_events == NULL) {
            throw std::runtime_error("Failed to open events file for writing: " + events_path);
        }
    }
    log_event("btsniffer run started");
    log_event(boost::format("config driver=%s center_mhz=%.3f bandwidth_mhz=%u rate=%.0f bins=%u seconds=%.3f fifo=%s show_init_failed=%u record_only=%u")
              % driver % freq_mhz % bins % rate % bins % seconds
              % (fifo_path.empty() ? "(disabled)" : fifo_path)
              % (g_show_init_failed ? 1 : 0)
              % (g_record_only ? 1 : 0));
    emit_json_event((boost::format("{\"time_us\":%lld,\"type\":\"config\",\"driver\":%s,\"center_mhz\":%.3f,\"bandwidth_mhz\":%u,\"sample_rate\":%.0f,\"bins\":%u,\"record_only\":%s}")
                     % now_us() % json_quote(driver) % freq_mhz % bins % rate % bins % (g_record_only ? "true" : "false")).str());
    fec23_init();
    log_event("fec23 decoder initialized");
    if (legacy_rate_set) {
        log_event(boost::format("deprecated rate alias converted rate_mhz=%.3f") % legacy_rate_mhz);
    }
    if (seconds <= 0.0) {
        throw std::runtime_error("--seconds must be positive");
    }
    if (g_record_only && fifo_path.empty()) {
        throw std::runtime_error("--record-only requires --fifo");
    }

    // 0. enumerate devices (list all devices' information)
    SoapySDR::KwargsList results = SoapySDR::Device::enumerate();
    SoapySDR::Kwargs::iterator it;

    for( int i = 0; i < results.size(); ++i)
    {
        printf("Found device #%d: ", i);
        for( it = results[i].begin(); it != results[i].end(); ++it)
        {
            printf("%s = %s\n", it->first.c_str(), it->second.c_str());
            log_event(boost::format("device[%d] %s=%s") % i % it->first % it->second);
        }
        printf("\n");
    }

    // 1. create device instance

    if (results.empty()) {
        throw std::runtime_error("No SoapySDR devices found");
    }

    SoapySDR::Kwargs args;
    bool found_requested_driver = false;
    for (size_t i = 0; i < results.size(); ++i) {
        SoapySDR::Kwargs::const_iterator driver_it = results[i].find("driver");
        if (driver_it != results[i].end() && driver_it->second == driver) {
            args = results[i];
            found_requested_driver = true;
            break;
        }
    }
    if (!found_requested_driver) {
        throw std::runtime_error("Requested SoapySDR driver not found: " + driver);
    }
    std::cout << "Opening SoapySDR driver=" << driver
              << " at " << (freq / 1.0e6) << " MHz, "
              << (rate / 1.0e6) << " Msps, "
              << decfactor << " bins" << std::endl;
    log_event(boost::format("opening driver=%s center_mhz=%.3f rate_msps=%.3f bins=%u")
              % driver % (freq / 1.0e6) % (rate / 1.0e6) % decfactor);

    //	1.2 make device
    SoapySDR::Device *sdr = SoapySDR::Device::make(args);

    if( sdr == NULL )
    {
        fprintf(stderr, "SoapySDR::Device::make failed\n");
        return EXIT_FAILURE;
    }

    // 2. query device info
    std::vector< std::string > str_list;	//string list

    //	2.1 antennas
    str_list = sdr->listAntennas( SOAPY_SDR_RX, 0);
//    printf("Rx antennas: ");
//    for(int i = 0; i < str_list.size(); ++i)
//        printf("%s,", str_list[i].c_str());
//    printf("\n");

    //	2.2 gains
    str_list = sdr->listGains( SOAPY_SDR_RX, 0);
//    printf("Rx Gains: ");
//    for(int i = 0; i < str_list.size(); ++i)
//        printf("%s, ", str_list[i].c_str());
//    printf("\n");

    //	2.3. ranges(frequency ranges)
    SoapySDR::RangeList ranges = sdr->getFrequencyRange( SOAPY_SDR_RX, 0);
//    printf("Rx freq ranges: ");
//    for(int i = 0; i < ranges.size(); ++i)
//        printf("[%g MHz -> %g MHz], ", ranges[i].minimum()/1.0e6, ranges[i].maximum()/1.0e6);
//    printf("\n");

    // 3. apply settings
    sdr->setSampleRate( SOAPY_SDR_RX, 0, rate);
    sdr->setFrequency( SOAPY_SDR_RX, 0, freq);
    sdr->setBandwidth( SOAPY_SDR_RX, 0, rate);
    str_list = sdr->listGains(SOAPY_SDR_RX, 0);
    if (std::find(str_list.begin(), str_list.end(), "LNA") != str_list.end())
        sdr->setGain(SOAPY_SDR_RX, 0, "LNA", 40);
    if (std::find(str_list.begin(), str_list.end(), "VGA") != str_list.end())
        sdr->setGain(SOAPY_SDR_RX, 0, "VGA", 36);
    if (std::find(str_list.begin(), str_list.end(), "AMP") != str_list.end())
        sdr->setGain(SOAPY_SDR_RX, 0, "AMP", 0);

    // 4. setup a stream matching iqsamp_t.
    SoapySDR::Stream *rx_stream = sdr->setupStream( SOAPY_SDR_RX, SOAPY_SDR_CF32);
    if( rx_stream == NULL)
    {
        fprintf( stderr, "Failed\n");
        SoapySDR::Device::unmake( sdr );
        return EXIT_FAILURE;
    }
    sdr->activateStream( rx_stream, 0, 0, 0);

    // 5. create a re-usable buffer for rx samples



    // Allocate data buffers.
    const double nseconds = seconds;
    const double bufsize_us = nseconds * rate;
    const size_t usrp_bufsize = 1024;
    const size_t usrp_nbuffers = (size_t) ceil(bufsize_us / (float) usrp_bufsize);
    std::vector<iqsamp_t> bankA(usrp_bufsize * usrp_nbuffers);
    std::vector<iqsamp_t> bankB(usrp_bufsize * usrp_nbuffers);
    iqsamp_t buff[usrp_bufsize];
    int flags;
    long long time_ns;
    iqsamp_t *bankptr;
    void *buffs[] = {buff};

    bool save_to_file = !fifo_path.empty();

    printf("usrp_bufsize  : %lu\n",usrp_bufsize );
    printf("usrp_nbuffers : %lu\n",usrp_nbuffers );


    // Auxiliary data for recv().
    double timeout = 10.0;
    unsigned long num_acc_samps = 0;
    unsigned long num_rx_samps = 0;

    // Create thread for processing data unless this run is a pure IQ recorder.
    pthread_t proc_thread;
    proc_pars_t proc_pars = {&bankA.front(), &bankB.front(), usrp_bufsize/decfactor * usrp_nbuffers};
    bool proc_thread_started = false;
    if (!g_record_only) {
        pthread_create(&proc_thread, NULL, proc_routine, &proc_pars);
        proc_thread_started = true;
    }

    std::signal(SIGINT, &sigint_handler);

    // Start streaming.
    //    rx_stream->issue_stream_cmd(stream_cmd);
    std::cout << std::endl << "Streaming... Press CTRL+C to stop." << std::endl;
    std::cout << std::endl << "      Timestamp       LAP    Info " << std::endl;
    log_event("streaming started");
    bufselect = 0;
    bankptr = &bankA.front();
    printf("main: selecting bank A\n");
    FILE* fp = NULL;
//    printf("%d\n",bankptr);
    if(save_to_file){
        printf("Saving IQ samples to %s\n", fifo_path.c_str());
        fp = fopen(fifo_path.c_str(),"wb");
        if(fp == NULL){
            printf("# Failed to open the file %s for writing.. exiting!\n", fifo_path.c_str());
            return 1;
        }

        const std::string meta_path = fifo_path + ".meta";
        FILE *meta_fp = fopen(meta_path.c_str(), "w");
        if (meta_fp != NULL) {
            struct timeval record_start;
            gettimeofday(&record_start, NULL);
            fprintf(meta_fp, "{\n");
            fprintf(meta_fp, "  \"format\": \"cf32_le\",\n");
            fprintf(meta_fp, "  \"sample_type\": \"complex_float32_interleaved_iq\",\n");
            fprintf(meta_fp, "  \"center_hz\": %.0f,\n", freq);
            fprintf(meta_fp, "  \"sample_rate_hz\": %.0f,\n", rate);
            fprintf(meta_fp, "  \"bandwidth_hz\": %.0f,\n", rate);
            fprintf(meta_fp, "  \"bins\": %u,\n", decfactor);
            fprintf(meta_fp, "  \"driver\": \"%s\",\n", driver.c_str());
            fprintf(meta_fp, "  \"start_unix_sec\": %ld,\n", record_start.tv_sec);
            fprintf(meta_fp, "  \"start_unix_usec\": %ld,\n", record_start.tv_usec);
            fprintf(meta_fp, "  \"note\": \"Raw SoapySDR RX samples before channelization; use with btsniffer.log for timing.\"\n");
            fprintf(meta_fp, "}\n");
            fclose(meta_fp);
            log_event(boost::format("iq metadata written path=%s") % meta_path);
        } else {
            log_event(boost::format("iq metadata write failed path=%s") % meta_path);
        }
    }

//    bankptr = (bankptr + usrp_bufsize*n);
    while (stopsig == false) {

        //        bufselect = (bufselect + 1) % 2;
        //        printf("\nmain: bufselect %d \n",bufselect);
        //        // Lock buffer.
        //        pthread_spin_lock(&lock[bufselect]);

        //        // Claim buffer for writing.
        //        if (bufselect) {
        //            bankptr = &bankA.front();
        //            printf("main: selecting bank A\n");
        //        } else {
        //            bankptr = &bankB.front();
        //            printf("main: selecting bank B\n");
        //        }

        while (!g_record_only && !stopsig && bankA_ready.load()) {
            usleep(1000);
        }
        if (stopsig) break;

        bankptr = &bankA.front();
        // Receive data.
        size_t write_offset = 0;
        for (unsigned int n = 0; n < usrp_nbuffers; n++) {
            //            num_rx_samps = rx_stream->recv(bankptr + usrp_bufsize*n, usrp_bufsize, md, timeout, false);
            //            num_rx_samps = fread(bankptr + usrp_bufsize*n,usrp_bufsize,1, fp);

            //            num_rx_samps = sdr->readStream( rx_stream, buffs, usrp_bufsize, flags, time_ns, 1e5);
            //            memcpy(bankptr + usrp_bufsize*n, buffs,usrp_bufsize);


//            printf("n: %d\n",n);
            num_rx_samps = sdr->readStream( rx_stream, buffs, usrp_bufsize, flags, time_ns, 1e5);
            if ((long)num_rx_samps <= 0) {
                continue;
            }

            if (!g_record_only) {
                memcpy(bankptr + write_offset, buff, num_rx_samps * sizeof(iqsamp_t));
                write_offset += num_rx_samps;
                if (write_offset >= bankA.size()) {
                    break;
                }
            }

            if(save_to_file){
               fwrite(buff,sizeof(iqsamp_t),num_rx_samps,fp);
            }

//            bankptr = (bankptr + usrp_bufsize*n);

//            printf("sigbuf[0] : %f + i%f\n", (buff[0]).real(),
//                                             (buff[0]).imag());


            //            num_rx_samps = sdr->readStream( rx_stream,
            //                                            buffs,
            //                                            usrp_bufsize, flags, time_ns, 1e5);

            //            memcpy(bankptr + usrp_bufsize*n, buffs,usrp_bufsize);

            // Increment number of received samples.
            num_acc_samps += num_rx_samps;

        }
        if (!g_record_only && write_offset > 0) {
            bankA_ready.store(true);
        }
    }

    if (proc_thread_started) {
        pthread_join(proc_thread, NULL);
    }

    std::cout << "Done." << std::endl;
    log_event("streaming stopped");

    // 7. shutdown the stream
    sdr->deactivateStream( rx_stream, 0, 0);	//stop streaming
    sdr->closeStream( rx_stream );

    if(save_to_file && fp != NULL){
       fclose(fp);
    }

    // 8. cleanup device handle
    SoapySDR::Device::unmake( sdr );
    printf("Done\n");
    log_event("btsniffer run finished");
    fclose(g_log);
    g_log = NULL;

    return EXIT_SUCCESS;

}
