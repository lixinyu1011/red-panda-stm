/**************************************************************************/
/*
LTC2326-16 library for Teensy 4.1
*/
/**************************************************************************/

#include "Arduino.h"
#include <SPI.h>
#include "LTC2326_16.hpp"

/**************************************************************************/
/*
    Constructor
*/
/**************************************************************************/

LTC2326_16::LTC2326_16(byte cs, byte cnv, byte busy)
{
    pinMode(cs, OUTPUT);
    pinMode(cnv, OUTPUT);
    pinMode(busy, INPUT);
    digitalWrite(cs, HIGH);
    digitalWrite(cnv, LOW);
    _cs = cs;
    _cnv = cnv;
    _busy = busy;
}

/**************************************************************************/
/*
    Initiate a conversion.
*/
/**************************************************************************/

void LTC2326_16::convert()
{
    digitalWrite(_cnv, LOW);   // Ensure CNV is low first
    delayMicroseconds(1);      // t_CNVL min is 10ns, give it 1us
    digitalWrite(_cnv, HIGH);  // Rising edge starts conversion
    delayMicroseconds(2);      // t_CNVH min is 10ns, hold high for 2us
}

/**************************************************************************/
/*
    Check whether ADC is busy doing a conversion. Returns true if conversion
    is in progress (BUSY pin is HIGH), false otherwise.
*/
/**************************************************************************/

bool LTC2326_16::busy()
{
    bool status;
    status = (bool)digitalRead(_busy);
    return status;
}

/**************************************************************************/
/*
    Read the ADC data register.
*/
/**************************************************************************/

int16_t LTC2326_16::read()
{
    int16_t val;

    // Wait for conversion to complete - CNV must be high during conversion
    // Typical conversion time is 1.6us, max 3us
    delayMicroseconds(3);

    // Now pull CNV low to enable data output
    digitalWrite(_cnv, LOW);
    delayMicroseconds(1);    // t_CNVL min is 10ns

    SPI1.beginTransaction(_spi_settings);
    digitalWrite(_cs, LOW);  // Select chip (RDL/CS active low enables SDO)
    delayMicroseconds(1);    // t_CSLSDO min is 0ns, but give it time
    val = SPI1.transfer16(0x0000);
    digitalWrite(_cs, HIGH); // Deselect chip
    SPI1.endTransaction();

    return val;
}

float LTC2326_16::read_volts()
{
    int16_t val = read();
    return val * _ref_buffer_volts;
}