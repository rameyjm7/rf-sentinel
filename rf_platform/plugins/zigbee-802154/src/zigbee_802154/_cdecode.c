#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define SYMBOL_CHIPS 32

static const char *PRIMARY_TABLE[16] = {
    "11011001110000110101001000101110",
    "11101101100111000011010100100010",
    "00101110110110011100001101010010",
    "00100010111011011001110000110101",
    "01010010001011101101100111000011",
    "00110101001000101110110110011100",
    "11000011010100100010111011011001",
    "10011100001101010010001011101101",
    "10001100100101100000011101111011",
    "10111000110010010110000001110111",
    "01111011100011001001011000000111",
    "01110111101110001100100101100000",
    "00000111011110111000110010010110",
    "01100000011101111011100011001001",
    "10010110000001110111101110001100",
    "11001001011000000111011110111000",
};

static char REVERSED_TABLE[16][SYMBOL_CHIPS + 1];
static char INVERTED_TABLE[16][SYMBOL_CHIPS + 1];
static char REVERSED_INVERTED_TABLE[16][SYMBOL_CHIPS + 1];
static int tables_ready = 0;

static void build_tables(void) {
    if (tables_ready) {
        return;
    }
    for (int symbol = 0; symbol < 16; symbol++) {
        for (int i = 0; i < SYMBOL_CHIPS; i++) {
            char bit = PRIMARY_TABLE[symbol][i];
            REVERSED_TABLE[symbol][i] = PRIMARY_TABLE[symbol][SYMBOL_CHIPS - 1 - i];
            INVERTED_TABLE[symbol][i] = bit == '1' ? '0' : '1';
            char reversed_bit = PRIMARY_TABLE[symbol][SYMBOL_CHIPS - 1 - i];
            REVERSED_INVERTED_TABLE[symbol][i] = reversed_bit == '1' ? '0' : '1';
        }
        REVERSED_TABLE[symbol][SYMBOL_CHIPS] = '\0';
        INVERTED_TABLE[symbol][SYMBOL_CHIPS] = '\0';
        REVERSED_INVERTED_TABLE[symbol][SYMBOL_CHIPS] = '\0';
    }
    tables_ready = 1;
}

static const char *symbol_bits(const char *table_name, int symbol) {
    build_tables();
    if (strcmp(table_name, "primary") == 0) {
        return PRIMARY_TABLE[symbol];
    }
    if (strcmp(table_name, "reversed") == 0) {
        return REVERSED_TABLE[symbol];
    }
    if (strcmp(table_name, "inverted") == 0) {
        return INVERTED_TABLE[symbol];
    }
    if (strcmp(table_name, "reversed_inverted") == 0) {
        return REVERSED_INVERTED_TABLE[symbol];
    }
    return NULL;
}

static PyObject *nearest_symbol_iq(PyObject *self, PyObject *args) {
    PyObject *iq_object = NULL;
    int chip_samples = 0;
    const char *table_name = NULL;
    if (!PyArg_ParseTuple(args, "Ois", &iq_object, &chip_samples, &table_name)) {
        return NULL;
    }
    if (chip_samples <= 0) {
        Py_RETURN_NONE;
    }

    Py_buffer iq_view;
    if (PyObject_GetBuffer(iq_object, &iq_view, PyBUF_CONTIG_RO) != 0) {
        return NULL;
    }

    const Py_ssize_t expected_samples = (SYMBOL_CHIPS + 1) * (Py_ssize_t)chip_samples;
    const Py_ssize_t expected_bytes = expected_samples * 2 * (Py_ssize_t)sizeof(float);
    if (iq_view.len != expected_bytes) {
        PyBuffer_Release(&iq_view);
        Py_RETURN_NONE;
    }

    const float *iq = (const float *)iq_view.buf;
    double iq_power = 0.0;
    for (Py_ssize_t sample = 0; sample < expected_samples; sample++) {
        const double real = (double)iq[2 * sample];
        const double imag = (double)iq[(2 * sample) + 1];
        iq_power += (real * real) + (imag * imag);
    }
    const double iq_norm = sqrt(iq_power) + 1e-9;
    const int pulse_samples = 2 * chip_samples;

    const char *table[16];
    for (int symbol = 0; symbol < 16; symbol++) {
        table[symbol] = symbol_bits(table_name, symbol);
        if (table[symbol] == NULL) {
            PyBuffer_Release(&iq_view);
            PyErr_SetString(PyExc_ValueError, "unknown symbol table");
            return NULL;
        }
    }

    int best_symbol = -1;
    double best_score = -1.0e300;
    Py_BEGIN_ALLOW_THREADS
    for (int symbol = 0; symbol < 16; symbol++) {
        const char *bits = table[symbol];
        double dot = 0.0;
        double ref_power = 0.0;
        for (int chip = 0; chip < SYMBOL_CHIPS; chip++) {
            const double sign = bits[chip] == '1' ? 1.0 : -1.0;
            const Py_ssize_t start = (Py_ssize_t)chip * chip_samples;
            for (int offset = 0; offset < pulse_samples; offset++) {
                const Py_ssize_t sample = start + offset;
                if (sample >= expected_samples) {
                    break;
                }
                const double pulse = sin(M_PI * (((double)offset + 0.5) / (double)pulse_samples));
                const double ref = sign * pulse;
                ref_power += ref * ref;
                if ((chip & 1) == 0) {
                    dot += ref * (double)iq[2 * sample];
                } else {
                    dot += ref * (double)iq[(2 * sample) + 1];
                }
            }
        }
        const double score = dot / ((sqrt(ref_power) + 1e-9) * iq_norm);
        if (score > best_score) {
            best_score = score;
            best_symbol = symbol;
        }
    }
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&iq_view);
    if (best_symbol < 0) {
        Py_RETURN_NONE;
    }
    double quality = best_score;
    if (quality < -1.0) {
        quality = -1.0;
    } else if (quality > 1.0) {
        quality = 1.0;
    }
    const double positive_quality = quality > 0.0 ? quality : 0.0;
    const int pseudo_distance = (int)llround((1.0 - positive_quality) * (double)SYMBOL_CHIPS);
    return Py_BuildValue("iis", best_symbol, pseudo_distance, table[best_symbol]);
}

static int score_nearest_symbol_iq(
    const float *iq,
    Py_ssize_t input_samples,
    Py_ssize_t start_sample,
    int chip_samples,
    const char *table[16],
    int *out_symbol,
    int *out_distance
) {
    const Py_ssize_t expected_samples = (SYMBOL_CHIPS + 1) * (Py_ssize_t)chip_samples;
    if (chip_samples <= 0 || start_sample < 0 || start_sample + expected_samples > input_samples) {
        return 0;
    }

    double iq_power = 0.0;
    for (Py_ssize_t sample = 0; sample < expected_samples; sample++) {
        const Py_ssize_t index = start_sample + sample;
        const double real = (double)iq[2 * index];
        const double imag = (double)iq[(2 * index) + 1];
        iq_power += (real * real) + (imag * imag);
    }
    const double iq_norm = sqrt(iq_power) + 1e-9;
    const int pulse_samples = 2 * chip_samples;

    int best_symbol = -1;
    double best_score = -1.0e300;
    for (int symbol = 0; symbol < 16; symbol++) {
        const char *bits = table[symbol];
        double dot = 0.0;
        double ref_power = 0.0;
        for (int chip = 0; chip < SYMBOL_CHIPS; chip++) {
            const double sign = bits[chip] == '1' ? 1.0 : -1.0;
            const Py_ssize_t chip_start = start_sample + ((Py_ssize_t)chip * (Py_ssize_t)chip_samples);
            for (int offset = 0; offset < pulse_samples; offset++) {
                const Py_ssize_t sample = chip_start + offset;
                if (sample >= start_sample + expected_samples) {
                    break;
                }
                const double pulse = sin(M_PI * (((double)offset + 0.5) / (double)pulse_samples));
                const double ref = sign * pulse;
                ref_power += ref * ref;
                if ((chip & 1) == 0) {
                    dot += ref * (double)iq[2 * sample];
                } else {
                    dot += ref * (double)iq[(2 * sample) + 1];
                }
            }
        }
        const double score = dot / ((sqrt(ref_power) + 1e-9) * iq_norm);
        if (score > best_score) {
            best_score = score;
            best_symbol = symbol;
        }
    }

    if (best_symbol < 0) {
        return 0;
    }
    double quality = best_score;
    if (quality < -1.0) {
        quality = -1.0;
    } else if (quality > 1.0) {
        quality = 1.0;
    }
    const double positive_quality = quality > 0.0 ? quality : 0.0;
    *out_symbol = best_symbol;
    *out_distance = (int)llround((1.0 - positive_quality) * (double)SYMBOL_CHIPS);
    return 1;
}

static PyObject *nearest_symbols_iq_bulk(PyObject *self, PyObject *args) {
    PyObject *iq_object = NULL;
    int chip_samples = 0;
    const char *table_name = NULL;
    Py_ssize_t start_sample = 0;
    int symbol_count = 0;
    if (!PyArg_ParseTuple(args, "Oisni", &iq_object, &chip_samples, &table_name, &start_sample, &symbol_count)) {
        return NULL;
    }
    if (chip_samples <= 0 || symbol_count <= 0) {
        Py_RETURN_NONE;
    }

    Py_buffer iq_view;
    if (PyObject_GetBuffer(iq_object, &iq_view, PyBUF_CONTIG_RO) != 0) {
        return NULL;
    }

    if (iq_view.len < 2 * (Py_ssize_t)sizeof(float)) {
        PyBuffer_Release(&iq_view);
        Py_RETURN_NONE;
    }

    const char *table[16];
    for (int symbol = 0; symbol < 16; symbol++) {
        table[symbol] = symbol_bits(table_name, symbol);
        if (table[symbol] == NULL) {
            PyBuffer_Release(&iq_view);
            PyErr_SetString(PyExc_ValueError, "unknown symbol table");
            return NULL;
        }
    }

    PyObject *symbols = PyBytes_FromStringAndSize(NULL, symbol_count);
    PyObject *distances = PyBytes_FromStringAndSize(NULL, symbol_count);
    if (symbols == NULL || distances == NULL) {
        Py_XDECREF(symbols);
        Py_XDECREF(distances);
        PyBuffer_Release(&iq_view);
        return NULL;
    }

    const float *iq = (const float *)iq_view.buf;
    const Py_ssize_t input_samples = iq_view.len / (2 * (Py_ssize_t)sizeof(float));
    const Py_ssize_t symbol_stride = SYMBOL_CHIPS * (Py_ssize_t)chip_samples;
    unsigned char *symbol_bytes = (unsigned char *)PyBytes_AS_STRING(symbols);
    unsigned char *distance_bytes = (unsigned char *)PyBytes_AS_STRING(distances);
    int ok = 1;

    Py_BEGIN_ALLOW_THREADS
    for (int index = 0; index < symbol_count; index++) {
        int symbol = -1;
        int distance = 255;
        const Py_ssize_t offset = start_sample + ((Py_ssize_t)index * symbol_stride);
        if (!score_nearest_symbol_iq(iq, input_samples, offset, chip_samples, table, &symbol, &distance)) {
            ok = 0;
            break;
        }
        symbol_bytes[index] = (unsigned char)(symbol & 0x0f);
        distance_bytes[index] = (unsigned char)(distance < 0 ? 0 : (distance > 255 ? 255 : distance));
    }
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&iq_view);
    if (!ok) {
        Py_DECREF(symbols);
        Py_DECREF(distances);
        Py_RETURN_NONE;
    }
    return Py_BuildValue("NN", symbols, distances);
}

static PyObject *channelize_boxcar(PyObject *self, PyObject *args) {
    PyObject *iq_object = NULL;
    double freq_offset_hz = 0.0;
    double sample_rate_sps = 0.0;
    long long sample_offset = 0;
    int decimation = 0;
    int taps = 0;
    if (!PyArg_ParseTuple(args, "OddLii", &iq_object, &freq_offset_hz, &sample_rate_sps, &sample_offset, &decimation, &taps)) {
        return NULL;
    }
    if (sample_rate_sps <= 0.0 || decimation <= 0 || taps <= 0) {
        Py_RETURN_NONE;
    }

    Py_buffer iq_view;
    if (PyObject_GetBuffer(iq_object, &iq_view, PyBUF_CONTIG_RO) != 0) {
        return NULL;
    }
    if (iq_view.len < 2 * (Py_ssize_t)sizeof(float)) {
        PyBuffer_Release(&iq_view);
        return PyBytes_FromStringAndSize("", 0);
    }

    const Py_ssize_t input_samples = iq_view.len / (2 * (Py_ssize_t)sizeof(float));
    const Py_ssize_t output_samples = input_samples / decimation;
    if (output_samples <= 0) {
        PyBuffer_Release(&iq_view);
        return PyBytes_FromStringAndSize("", 0);
    }

    PyObject *out = PyBytes_FromStringAndSize(NULL, output_samples * 2 * (Py_ssize_t)sizeof(float));
    if (out == NULL) {
        PyBuffer_Release(&iq_view);
        return NULL;
    }

    const float *iq = (const float *)iq_view.buf;
    float *dst = (float *)PyBytes_AS_STRING(out);
    const int half_taps = taps / 2;
    const double phase_step = -2.0 * M_PI * freq_offset_hz / sample_rate_sps;

    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t out_index = 0; out_index < output_samples; out_index++) {
        const Py_ssize_t center = out_index * (Py_ssize_t)decimation;
        double acc_re = 0.0;
        double acc_im = 0.0;
        int count = 0;
        for (int tap = 0; tap < taps; tap++) {
            const Py_ssize_t source = center + (Py_ssize_t)tap - (Py_ssize_t)half_taps;
            if (source < 0 || source >= input_samples) {
                continue;
            }
            const double phase = phase_step * ((double)sample_offset + (double)source);
            const double c = cos(phase);
            const double s = sin(phase);
            const double re = (double)iq[2 * source];
            const double im = (double)iq[(2 * source) + 1];
            acc_re += (re * c) - (im * s);
            acc_im += (re * s) + (im * c);
            count++;
        }
        if (count > 0) {
            acc_re /= (double)count;
            acc_im /= (double)count;
        }
        dst[2 * out_index] = (float)acc_re;
        dst[(2 * out_index) + 1] = (float)acc_im;
    }
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&iq_view);
    return out;
}

static PyObject *correct_frequency_offset(PyObject *self, PyObject *args) {
    PyObject *iq_object = NULL;
    double sample_rate_sps = 0.0;
    double frequency_offset_hz = 0.0;
    if (!PyArg_ParseTuple(args, "Odd", &iq_object, &sample_rate_sps, &frequency_offset_hz)) {
        return NULL;
    }
    if (sample_rate_sps <= 0.0) {
        Py_RETURN_NONE;
    }

    Py_buffer iq_view;
    if (PyObject_GetBuffer(iq_object, &iq_view, PyBUF_CONTIG_RO) != 0) {
        return NULL;
    }
    if (iq_view.len < 2 * (Py_ssize_t)sizeof(float)) {
        PyBuffer_Release(&iq_view);
        return PyBytes_FromStringAndSize("", 0);
    }

    const Py_ssize_t input_samples = iq_view.len / (2 * (Py_ssize_t)sizeof(float));
    PyObject *out = PyBytes_FromStringAndSize(NULL, input_samples * 2 * (Py_ssize_t)sizeof(float));
    if (out == NULL) {
        PyBuffer_Release(&iq_view);
        return NULL;
    }

    const float *iq = (const float *)iq_view.buf;
    float *dst = (float *)PyBytes_AS_STRING(out);
    const double phase_step = -2.0 * M_PI * frequency_offset_hz / sample_rate_sps;
    const double step_c = cos(phase_step);
    const double step_s = sin(phase_step);

    Py_BEGIN_ALLOW_THREADS
    double osc_re = 1.0;
    double osc_im = 0.0;
    for (Py_ssize_t index = 0; index < input_samples; index++) {
        const double re = (double)iq[2 * index];
        const double im = (double)iq[(2 * index) + 1];
        dst[2 * index] = (float)((re * osc_re) - (im * osc_im));
        dst[(2 * index) + 1] = (float)((re * osc_im) + (im * osc_re));

        const double next_re = (osc_re * step_c) - (osc_im * step_s);
        const double next_im = (osc_re * step_s) + (osc_im * step_c);
        osc_re = next_re;
        osc_im = next_im;
        if ((index & 1023) == 1023) {
            const double norm = sqrt((osc_re * osc_re) + (osc_im * osc_im)) + 1e-18;
            osc_re /= norm;
            osc_im /= norm;
        }
    }
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&iq_view);
    return out;
}

static PyMethodDef methods[] = {
    {"nearest_symbol_iq", nearest_symbol_iq, METH_VARARGS, "Return the nearest 802.15.4 waveform symbol."},
    {"nearest_symbols_iq_bulk", nearest_symbols_iq_bulk, METH_VARARGS, "Return nearest 802.15.4 waveform symbols for a contiguous symbol stream."},
    {"channelize_boxcar", channelize_boxcar, METH_VARARGS, "Native frequency shift and boxcar decimation for wideband channelization."},
    {"correct_frequency_offset", correct_frequency_offset, METH_VARARGS, "Native complex frequency correction for complex64 IQ."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_cdecode",
    "Native accelerators for zigbee_802154 decoding.",
    -1,
    methods,
};

PyMODINIT_FUNC PyInit__cdecode(void) {
    return PyModule_Create(&module);
}
