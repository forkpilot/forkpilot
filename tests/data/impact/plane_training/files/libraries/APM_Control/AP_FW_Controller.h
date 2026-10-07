





class AP_FW_Controller
{














    void set_ff_scale(float _ff_scale) { ff_scale = _ff_scale; }
















    {

    }


    void set_notch_sample_rate(float sample_rate) { rate_pid.set_notch_sample_rate(sample_rate); }

    AP_Float &kP(void) { return rate_pid.kP(); }
    AP_Float &kI(void) { return rate_pid.kI(); }
    AP_Float &kD(void) { return rate_pid.kD(); }
    AP_Float &kFF(void) { return rate_pid.ff(); }
    AP_Float &tau(void) { return gains.tau; }





    float get_angle_error_deg() const { return angle_err_deg; }













































    virtual float get_rate_target_offset() const { return 0.0; }




















};
